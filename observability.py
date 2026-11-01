"""
Observability
=============
Structured logging plus a dependency-free Prometheus metrics registry.

Text format (default) stays human-readable on a console; `NP_LOG_FORMAT=json`
emits one JSON object per line with the active job / request id attached, which
is what log shippers expect.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

# ── Request-scoped context ────────────────────────────────────────────────────

_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def set_job_id(job_id: Optional[str]):
    return _job_id.set(job_id or "")


def reset_job_id(token) -> None:
    _job_id.reset(token)


def current_job_id() -> str:
    return _job_id.get()


def set_request_id(request_id: Optional[str]):
    return _request_id.set(request_id or "")


def reset_request_id(token) -> None:
    _request_id.reset(token)


# ── Logging ───────────────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """One JSON object per log record, with job/request ids when available."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts":     datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        if job_id := _job_id.get():
            payload["job_id"] = job_id
        if request_id := _request_id.get():
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler()
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s — %(message)s", "%H:%M:%S",
        ))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn installs its own handlers; route them through the same config.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


# ── Metrics ───────────────────────────────────────────────────────────────────

def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(float(value))


class Metrics:
    """Minimal Prometheus text-format registry (no third-party dependency)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._started = time.time()

    def declare(self, name: str, kind: str, doc: str,
                labels: Sequence[str] = ()) -> None:
        with self._lock:
            self._meta.setdefault(name, (kind, doc, tuple(labels)))

    def _key(self, name: str, labels: Optional[Mapping[str, object]]):
        meta = self._meta.get(name)
        if meta is None:
            raise KeyError(f"metric '{name}' was not declared")
        pairs = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        expected = set(meta[2])
        if {k for k, _ in pairs} != expected:
            raise ValueError(f"metric '{name}' expects labels {sorted(expected)}")
        return (name, pairs)

    def inc(self, name: str, labels: Optional[Mapping[str, object]] = None,
            value: float = 1.0) -> None:
        with self._lock:
            key = self._key(name, labels)
            self._values[key] = self._values.get(key, 0.0) + value

    def dec(self, name: str, labels: Optional[Mapping[str, object]] = None,
            value: float = 1.0) -> None:
        self.inc(name, labels, -value)

    def set(self, name: str, value: float,
            labels: Optional[Mapping[str, object]] = None) -> None:
        with self._lock:
            self._values[self._key(name, labels)] = float(value)

    def render(self) -> str:
        with self._lock:
            self._values[("np_process_uptime_seconds", ())] = time.time() - self._started
            lines: list[str] = []
            for name in sorted(self._meta):
                kind, doc, _labels = self._meta[name]
                lines.append(f"# HELP {name} {doc}")
                lines.append(f"# TYPE {name} {kind}")
                for (metric, pairs), value in sorted(self._values.items()):
                    if metric != name:
                        continue
                    label_str = ""
                    if pairs:
                        label_str = "{" + ",".join(
                            f'{k}="{_escape(v)}"' for k, v in pairs
                        ) + "}"
                    lines.append(f"{name}{label_str} {_format_value(value)}")
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._values.clear()
            self._started = time.time()


METRICS = Metrics()
METRICS.declare("np_jobs_total", "counter",
                "Provisioning jobs by final status", ("status",))
METRICS.declare("np_steps_total", "counter",
                "Provisioning steps by result", ("result",))
METRICS.declare("np_rollback_total", "counter",
                "Rollback attempts by result", ("result",))
METRICS.declare("np_http_requests_total", "counter",
                "HTTP requests by method, route and status",
                ("method", "route", "status"))
METRICS.declare("np_jobs_in_flight", "gauge",
                "Provisioning jobs currently executing")
METRICS.declare("np_jobs_pending", "gauge",
                "Approved jobs waiting for a worker")
METRICS.declare("np_jobs_claimed_total", "counter",
                "Provisioning jobs claimed from the durable queue")
METRICS.declare("np_rollbacks_claimed_total", "counter",
                "Deferred rollbacks claimed from the durable queue")
METRICS.declare("np_jobs_reclaimed_total", "counter",
                "Jobs or rollbacks recovered after a worker lease expired")
METRICS.declare("np_inventory_devices", "gauge",
                "Devices recorded in the inventory")
METRICS.declare("np_inventory_vlans", "gauge",
                "VLAN records in the inventory")
METRICS.declare("np_process_uptime_seconds", "gauge",
                "Seconds since the process started")


def _seed_defaults() -> None:
    """Emit zero-valued series so dashboards see metrics before the first event."""
    for status in (
        "pending_approval", "pending", "running", "success",
        "failed", "rolledback", "cancelled",
    ):
        METRICS.set("np_jobs_total", 0, {"status": status})
    for result in ("ok", "failed"):
        METRICS.set("np_steps_total", 0, {"result": result})
        METRICS.set("np_rollback_total", 0, {"result": result})
    METRICS.set("np_jobs_in_flight", 0)
    METRICS.set("np_inventory_devices", 0)
    METRICS.set("np_inventory_vlans", 0)
    METRICS.set("np_process_uptime_seconds", 0)


_seed_defaults()
