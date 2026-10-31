"""
NetProvision API
================
POST /api/vlans/plan                 — dry run: commands + warnings, no device I/O
POST /api/vlans/provision            — provision a VLAN across switches
GET  /api/vlans/provision/{job_id}   — job status + per-step evidence
POST /api/vlans/provision/{job_id}/rollback — revert a finished job

GET  /api/inventory/devices              — device list
GET  /api/inventory/devices/{hostname}   — device detail
GET  /api/inventory/devices/{hostname}/interfaces
GET  /api/inventory/devices/{hostname}/vlans
GET  /api/inventory/vlans                — all VLANs across all devices
GET  /api/inventory/vlans/{vlan_id}      — where a VLAN exists
GET  /api/inventory/interfaces/vlan/{vlan_id} — ports using this VLAN
POST /api/inventory/refresh/{hostname}   — re-collect device inventory

GET  /api/jobs                    — paginated job summaries
GET  /api/jobs/{job_id}           — full job detail
GET  /healthz                     — liveness
GET  /readyz                      — readiness (store reachable)
GET  /metrics                     — Prometheus text format
GET  /                            — web dashboard
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from typing import Optional

try:
    from fastapi import (
        BackgroundTasks,
        FastAPI,
        Header,
        HTTPException,
        Query,
        Request,
        Response,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
    from pydantic import BaseModel, Field
    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when fastapi is absent
    FASTAPI_AVAILABLE = False

from inventory.store import InventoryStore
from models import (
    DeviceCredentials,
    JobStatus,
    OperationStep,
    ProvisioningJob,
    VLANProvisionRequest,
)
from observability import (
    METRICS,
    reset_request_id,
    set_request_id,
)
from provisioner.engine import (
    VLAN_MAX,
    execute,
    inspect_request,
    rollback,
    validate,
)

APP_VERSION = "0.2.0"
IDEMPOTENCY_KEY_RE = r"^[A-Za-z0-9_.:\-]{1,64}$"


# ── Pydantic request bodies ───────────────────────────────────────────────────

if FASTAPI_AVAILABLE:
    class ProvisionRequest(BaseModel):
        vlan_id:       int        = Field(..., ge=2, le=VLAN_MAX)
        name:          str        = Field(..., min_length=1, max_length=32)
        switches:      list[str]  = Field(default_factory=list)
        add_to_trunks: list[str]  = Field(default_factory=list)
        access_ports:  list[str]  = Field(default_factory=list)
        description:   str        = ""
        requested_by:  str        = "api"


def _to_request(body: "ProvisionRequest") -> VLANProvisionRequest:
    return VLANProvisionRequest(
        vlan_id       = body.vlan_id,
        name          = body.name,
        switches      = body.switches,
        add_to_trunks = body.add_to_trunks,
        access_ports  = body.access_ports,
        description   = body.description,
        requested_by  = body.requested_by,
    )


if FASTAPI_AVAILABLE:
    class Utf8JSONResponse(JSONResponse):
        """Explicit charset so older HTTP clients decode non-ASCII text correctly."""

        media_type = "application/json; charset=utf-8"


def build_app(store: InventoryStore, device_map: dict[str, DeviceCredentials]) -> "FastAPI":
    if not FASTAPI_AVAILABLE:
        raise RuntimeError("pip install fastapi uvicorn")

    app = FastAPI(
        title="NetProvision",
        version=APP_VERSION,
        default_response_class=Utf8JSONResponse,
    )

    origins = [o.strip() for o in os.getenv("NP_CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )

    # ── Observability middleware ──────────────────────────────────────────────

    @app.middleware("http")
    async def observe(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = set_request_id(request_id)
        status_code = "500"
        try:
            response = await call_next(request)
            status_code = str(response.status_code)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", None) or request.url.path
            METRICS.inc("np_http_requests_total", {
                "method": request.method, "route": path, "status": status_code,
            })
            reset_request_id(token)

    # ── Provisioning — dry run ────────────────────────────────────────────────

    @app.post("/api/vlans/plan")
    def plan_vlan(body: "ProvisionRequest"):
        """Build the exact command plan and warnings without touching a device."""
        req = _to_request(body)
        errors = validate(req, store, device_map)
        if errors:
            raise HTTPException(422, detail={"errors": errors})

        steps, warnings = inspect_request(req, store)
        return {
            "vlan_id":          req.vlan_id,
            "name":             req.name,
            "touches_devices":  False,
            "step_count":       len(steps),
            "steps":            [_plan_step(i, s) for i, s in enumerate(steps, start=1)],
            "warnings":         warnings,
        }

    # ── Provisioning — apply ──────────────────────────────────────────────────

    @app.post("/api/vlans/provision", status_code=202)
    def provision_vlan(
        body:             "ProvisionRequest",
        background_tasks: BackgroundTasks,
        response:         Response,
        idempotency_key:  Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        if idempotency_key is not None and not _valid_idempotency_key(idempotency_key):
            raise HTTPException(400, "Idempotency-Key must be 1-64 characters of [A-Za-z0-9_.:-]")

        if idempotency_key:
            existing = store.get_job_by_idempotency_key(idempotency_key)
            if existing:
                response.status_code = 200
                return {**_job_to_dict(existing), "replayed": True}

        req = _to_request(body)
        errors = validate(req, store, device_map)
        if errors:
            raise HTTPException(422, detail={"errors": errors})

        steps, warnings = inspect_request(req, store)
        job = ProvisioningJob(
            job_id      = str(uuid.uuid4()),
            request     = req,
            status      = JobStatus.PENDING,
            steps       = steps,
            created_at  = datetime.now(timezone.utc),
            steps_total = len(steps),
        )

        try:
            store.write_job(job, idempotency_key=idempotency_key)
        except sqlite3.IntegrityError:
            # Lost a race with an identical request: replay the winner.
            existing = store.get_job_by_idempotency_key(idempotency_key) if idempotency_key else None
            if not existing:
                raise
            response.status_code = 200
            return {**_job_to_dict(existing), "replayed": True}

        background_tasks.add_task(execute, job, store, device_map)

        return {
            "job_id":    job.job_id,
            "status":    job.status.value,
            "steps":     len(steps),
            "warnings":  warnings,
            "message":   f"Provisioning VLAN {req.vlan_id} ({req.name}) — {len(steps)} steps queued",
        }

    @app.get("/api/vlans/provision/{job_id}")
    def get_job_status(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, f"Job '{job_id}' not found")
        return _job_to_dict(job)

    @app.post("/api/vlans/provision/{job_id}/rollback")
    def rollback_job(job_id: str, background_tasks: BackgroundTasks):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, f"Job '{job_id}' not found")
        if job.status not in (JobStatus.FAILED, JobStatus.SUCCESS):
            raise HTTPException(
                409,
                f"Job is in state '{job.status.value}' — only FAILED or SUCCESS jobs "
                f"can be rolled back",
            )
        background_tasks.add_task(rollback, job, store, device_map)
        return {"job_id": job_id, "message": "Rollback queued"}

    # ── Inventory — devices ───────────────────────────────────────────────────

    @app.get("/api/inventory/devices")
    def list_devices():
        devices = store.list_devices()
        return [_device_summary(d, store) for d in devices]

    @app.get("/api/inventory/devices/{hostname}")
    def get_device(hostname: str):
        device = store.get_device(hostname)
        if not device:
            raise HTTPException(404, f"Device '{hostname}' not in inventory")
        ifaces = store.get_interfaces(hostname)
        vlans  = store.get_vlans(device=hostname)
        return {
            "hostname":    device.hostname,
            "ip":          device.ip,
            "device_type": device.device_type,
            "platform":    device.platform,
            "model":       device.model,
            "serial":      device.serial,
            "uptime":      device.uptime,
            "tags":        device.tags,
            "last_seen":   device.last_seen.isoformat() if device.last_seen else None,
            "interface_count": len(ifaces),
            "vlan_count":      len(vlans),
        }

    @app.get("/api/inventory/devices/{hostname}/interfaces")
    def get_device_interfaces(hostname: str):
        if not store.get_device(hostname):
            raise HTTPException(404, f"Device '{hostname}' not in inventory")
        return [_iface_to_dict(i) for i in store.get_interfaces(hostname)]

    @app.get("/api/inventory/devices/{hostname}/vlans")
    def get_device_vlans(hostname: str):
        if not store.get_device(hostname):
            raise HTTPException(404, f"Device '{hostname}' not in inventory")
        return [_vlan_to_dict(v) for v in store.get_vlans(device=hostname)]

    # ── Inventory — VLANs ─────────────────────────────────────────────────────

    @app.get("/api/inventory/vlans")
    def list_vlans(device: Optional[str] = None):
        return [_vlan_to_dict(v) for v in store.get_vlans(device=device)]

    @app.get("/api/inventory/vlans/{vlan_id}")
    def get_vlan(vlan_id: int):
        vlans = [v for v in store.get_vlans() if v.vlan_id == vlan_id]
        if not vlans:
            raise HTTPException(404, f"VLAN {vlan_id} not found in inventory")
        ifaces = store.find_interfaces_by_vlan(vlan_id)
        return {
            "vlan_id":    vlan_id,
            "name":       vlans[0].name,
            "devices":    sorted({v.device for v in vlans}),
            "interfaces": [_iface_to_dict(i) for i in ifaces],
        }

    @app.get("/api/inventory/interfaces/vlan/{vlan_id}")
    def interfaces_for_vlan(vlan_id: int):
        return [_iface_to_dict(i) for i in store.find_interfaces_by_vlan(vlan_id)]

    # ── Inventory refresh ─────────────────────────────────────────────────────

    @app.post("/api/inventory/refresh/{hostname}")
    def refresh_inventory(hostname: str, background_tasks: BackgroundTasks):
        creds = device_map.get(hostname)
        if not creds:
            raise HTTPException(404, f"Device '{hostname}' not in config")
        from inventory.collector import collect
        background_tasks.add_task(collect, creds, store)
        return {"hostname": hostname, "message": "Inventory refresh queued"}

    # ── Jobs ──────────────────────────────────────────────────────────────────

    @app.get("/api/jobs")
    def list_jobs(
        limit:         int = Query(50, ge=1, le=200),
        offset:        int = Query(0, ge=0),
        status_filter: Optional[str] = Query(None, alias="status"),
    ):
        if status_filter and status_filter not in {s.value for s in JobStatus}:
            raise HTTPException(400, f"Unknown status '{status_filter}'")
        jobs = store.list_jobs(limit=limit, offset=offset, status=status_filter)
        return {
            "total":  store.count_jobs(status=status_filter),
            "limit":  limit,
            "offset": offset,
            "jobs":   [_job_to_dict(j, include_steps=False) for j in jobs],
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, f"Job '{job_id}' not found")
        return _job_to_dict(job)

    # ── Health & metrics ──────────────────────────────────────────────────────

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "service": "netprovision", "version": APP_VERSION}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        try:
            stats = store.stats()
        except Exception as exc:
            raise HTTPException(503, f"inventory store unavailable: {exc}") from exc
        return {
            "status":             "ready",
            "configured_devices": len(device_map),
            "inventory":          stats,
        }

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        try:
            stats = store.stats()
            METRICS.set("np_inventory_devices", stats["devices"])
            METRICS.set("np_inventory_vlans", stats["vlans"])
        except Exception:  # metrics must never fail the scrape
            pass
        return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4")

    # ── Dashboard ─────────────────────────────────────────────────────────────
    web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
    if os.path.isdir(web_dir):
        @app.get("/", include_in_schema=False)
        def dashboard():
            return FileResponse(os.path.join(web_dir, "index.html"))

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _valid_idempotency_key(key: str) -> bool:
    return re.match(IDEMPOTENCY_KEY_RE, key) is not None


def _plan_step(index: int, step: OperationStep) -> dict:
    return {
        "index":            index,
        "device":           step.device,
        "operation":        step.operation.value,
        "interface":        step.interface,
        "vlan_id":          step.vlan_id,
        "commands":         step.command.splitlines(),
        "rollback_commands": step.rollback_cmd.splitlines() if step.rollback_cmd else [],
        "rollback_note":    step.rollback_note,
    }


def _device_summary(device, store):
    ifaces = store.get_interfaces(device.hostname)
    vlans  = store.get_vlans(device=device.hostname)
    return {
        "hostname":        device.hostname,
        "ip":              device.ip,
        "device_type":     device.device_type,
        "model":           device.model,
        "last_seen":       device.last_seen.isoformat() if device.last_seen else None,
        "tags":            device.tags,
        "interface_count": len(ifaces),
        "vlan_count":      len(vlans),
        "trunk_count":     sum(1 for i in ifaces if i.mode.value == "trunk"),
    }


def _iface_to_dict(iface):
    return {
        "device":      iface.device,
        "name":        iface.name,
        "description": iface.description,
        "mode":        iface.mode.value,
        "status":      iface.status.value,
        "access_vlan": iface.access_vlan,
        "trunk_vlans": iface.trunk_vlans,
        "native_vlan": iface.native_vlan,
        "ip_address":  iface.ip_address,
        "speed":       iface.speed,
    }


def _vlan_to_dict(vlan):
    return {
        "vlan_id":    vlan.vlan_id,
        "name":       vlan.name,
        "status":     vlan.status.value,
        "device":     vlan.device,
        "discovered": vlan.discovered.isoformat() if vlan.discovered else None,
    }


def _job_to_dict(job: ProvisioningJob, include_steps: bool = True) -> dict:
    data = {
        "job_id":          job.job_id,
        "vlan_id":         job.request.vlan_id,
        "vlan_name":       job.request.name,
        "switches":        job.request.switches,
        "add_to_trunks":   job.request.add_to_trunks,
        "access_ports":    job.request.access_ports,
        "requested_by":    job.request.requested_by,
        "status":          job.status.value,
        "steps_total":     job.steps_total or len(job.steps),
        "steps_attempted": job.steps_attempted,
        "steps_ok":        job.steps_succeeded,
        "steps_failed":    job.steps_failed,
        "created_at":      job.created_at.isoformat() if job.created_at else None,
        "completed_at":    job.completed_at.isoformat() if job.completed_at else None,
        "error":           job.error,
    }
    if include_steps:
        data["steps"] = [
            {
                "device":           s.device,
                "operation":        s.operation.value,
                "interface":        s.interface,
                "vlan_id":          s.vlan_id,
                "command":          s.command,
                "output":           s.output,
                "success":          s.success,
                "error":            s.error,
                "timestamp":        s.timestamp.isoformat() if s.timestamp else None,
                "rollback_cmd":     s.rollback_cmd,
                "rollback_note":    s.rollback_note,
                "rollback_applied": s.rollback_applied,
                "rollback_error":   s.rollback_error,
            }
            for s in job.steps
        ]
    return data
