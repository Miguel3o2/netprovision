"""
Durable Worker
==============
Runs provisioning jobs without holding them in the API process.

Inline mode (the default) executes jobs through FastAPI `BackgroundTasks`: fine
for a lab and for tests, but an API restart strands approved work with nothing
to run it, and only one API process can execute. This module is the durable option:

    API (NP_QUEUE_MODE=worker)          Worker (python worker.py)
      writes a PENDING job        →       claims it with a lease
      serves status from SQLite   ←       writes steps/evidence as it goes

Claims and leases live in SQLite, so the API and the worker can be separate
containers or services. A worker crash does not strand a job forever: the lease
expires and the next worker closes it out for human review.

Deliberate limits, so the guarantees are honest:

* One worker process. Per-device locks and VLAN claims are still in-process; two
  workers could target the same switch with two different jobs. Horizontal
  scaling needs Postgres plus distributed device locks.
* A claim is a lease, not a heartbeat that can distinguish "crashed" from
  "partitioned". An expired lease is therefore marked FAILED with instructions,
  not auto-rolled-back: the original worker may still be alive, and two writers
  on one switch is worse than a manual review. The stored steps and snapshot
  make `POST /api/vlans/provision/{id}/rollback` usable once a human checks.
* SQLite + WAL tolerates one host and a handful of processes. It is not a
  distributed queue; use a real one before deploying across hosts.

Environment:
  NP_QUEUE_MODE           inline | worker   (API side; default inline)
  NP_WORKER_ID            default hostname:pid
  NP_LEASE_SECONDS        default 300
  NP_WORKER_POLL_SECONDS  default 2
  NP_WORKER_RECLAIM       default true — close out expired leases
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
from typing import Optional

from inventory.store import InventoryStore
from models import DeviceCredentials, ProvisioningJob
from observability import METRICS
from provisioner.engine import execute, rollback

log = logging.getLogger(__name__)


class Worker:
    """Claims pending jobs one at a time and executes them."""

    def __init__(
        self,
        store:       InventoryStore,
        device_map:  dict[str, DeviceCredentials],
        *,
        worker_id:      Optional[str] = None,
        lease_seconds:  int = 300,
        poll_seconds:   float = 2.0,
        reclaim_expired: bool = True,
    ) -> None:
        self.store          = store
        self.device_map     = device_map
        self.worker_id      = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_seconds  = max(30, int(lease_seconds))
        self.poll_seconds   = max(0.2, float(poll_seconds))
        self.reclaim_expired = reclaim_expired
        self._stop           = threading.Event()


    def reclaim(self) -> int:
        """Close out jobs whose worker stopped heartbeating. Returns a count."""
        if not self.reclaim_expired:
            return 0

        reclaimed = 0
        for job in self.store.expired_leases():
            owner = job.lease_owner or "unknown"
            error = (
                f"worker lease expired (owner {owner}); device state is unknown — "
                f"review snapshot/steps and POST a rollback once verified"
            )
            if self.store.fail_interrupted_job(job.job_id, error):
                reclaimed += 1
                METRICS.inc("np_jobs_reclaimed_total")
                log.error(f"{job.job_id}: {error}")

        for job in self.store.expired_rollback_leases():
            if self.store.release_expired_rollback(job.job_id):
                reclaimed += 1
                METRICS.inc("np_jobs_reclaimed_total")
                log.warning(
                    f"{job.job_id}: stale rollback lease cleared; request will retry"
                )
        return reclaimed

    def run_once(self) -> bool:
        """
        Reclaim expired leases, then execute at most one pending job.

        Returns True when a job was processed, False when the queue was empty.
        """
        self.reclaim()

        job = self.store.claim_next_job(self.worker_id, self.lease_seconds)
        if job is not None:
            METRICS.inc("np_jobs_claimed_total")
            log.info(
                f"{job.job_id}: claimed by {self.worker_id} "
                f"(attempt {max(job.attempts, 1)}, lease {self.lease_seconds}s)"
            )
            self._execute(job)
            return True

        rollback_job = self.store.claim_rollback_job(
            self.worker_id, self.lease_seconds,
        )
        if rollback_job is not None:
            METRICS.inc("np_rollbacks_claimed_total")
            log.info(
                f"{rollback_job.job_id}: rollback claimed by {self.worker_id} "
                f"(requested by {rollback_job.rollback_requested_by or 'unknown'})"
            )
            self._execute_rollback(rollback_job)
            return True

        return False

    def _execute(self, job: ProvisioningJob) -> None:
        heartbeat = _Heartbeat(
            self.store, job.job_id, self.worker_id, self.lease_seconds,
        )
        heartbeat.start()
        try:
            execute(job, self.store, self.device_map)
        except Exception as exc:
            log.exception(f"{job.job_id}: worker failed while executing")
            self.store.fail_interrupted_job(job.job_id, f"worker error: {exc}")
        finally:
            heartbeat.stop()
            self.store.clear_lease(job.job_id, self.worker_id)
            if heartbeat.lost:
                log.error(
                    f"{job.job_id}: lease was lost while executing — another "
                    f"worker may reclaim this job"
                )

    def _execute_rollback(self, job: ProvisioningJob) -> None:
        heartbeat = _Heartbeat(
            self.store, job.job_id, self.worker_id, self.lease_seconds,
        )
        heartbeat.start()
        try:
            rollback(job, self.store, self.device_map)
        except Exception:
            log.exception(f"{job.job_id}: worker failed while rolling back")
        finally:
            heartbeat.stop()
            self.store.clear_lease(job.job_id, self.worker_id)


    def run_forever(self) -> None:
        log.info(
            f"worker {self.worker_id} started "
            f"(lease {self.lease_seconds}s, poll {self.poll_seconds}s, "
            f"reclaim={'on' if self.reclaim_expired else 'off'})"
        )
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(self.poll_seconds)
            except Exception:
                log.exception("worker loop error")
                self._stop.wait(min(self.poll_seconds * 5, 30))

        log.info(f"worker {self.worker_id} stopping")

    def stop(self) -> None:
        self._stop.set()


class _Heartbeat(threading.Thread):
    """Renews a job lease in the background while execute() works."""

    def __init__(
        self,
        store: InventoryStore,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        super().__init__(daemon=True, name=f"lease-{job_id[:8]}")
        self.store         = store
        self.job_id        = job_id
        self.worker_id     = worker_id
        self.lease_seconds = lease_seconds
        self.interval      = max(1.0, lease_seconds / 3)
        self.lost          = False
        self._stop_event   = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                if not self.store.heartbeat(
                    self.job_id, self.worker_id, self.lease_seconds,
                ):
                    self.lost = True
                    log.error(f"{self.job_id}: lease lost (expired or reassigned)")
                    return
            except Exception:
                log.exception(f"{self.job_id}: lease heartbeat failed")

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


def run_worker(
    config_path: str = "config.yaml",
    db_path: str = "inventory.db",
    *,
    once: bool = False,
    collect_first: bool = False,
    worker_id: Optional[str] = None,
    lease_seconds: Optional[int] = None,
    poll_seconds: Optional[float] = None,
) -> None:
    from main import load_config, load_devices

    raw = load_config(config_path)
    devices, device_map = load_devices(raw)

    store = InventoryStore(path=db_path)
    log.info(f"Inventory store: {db_path}")
    log.info(f"Loaded {len(devices)} device(s) from {config_path}")

    if collect_first and devices:
        from inventory.collector import collect
        log.info("Running initial inventory collection...")
        for creds in devices:
            collect(creds, store)

    worker = Worker(
        store,
        device_map,
        worker_id     = worker_id or os.getenv("NP_WORKER_ID") or None,
        lease_seconds = lease_seconds if lease_seconds is not None
                        else int(os.getenv("NP_LEASE_SECONDS", "300")),
        poll_seconds  = poll_seconds if poll_seconds is not None
                        else float(os.getenv("NP_WORKER_POLL_SECONDS", "2")),
        reclaim_expired = _env_flag("NP_WORKER_RECLAIM", True),
    )

    if once:
        processed = 0
        while worker.run_once():
            processed += 1
        log.info(f"drained {processed} job(s); exiting")
        return

    def _shutdown(signum, frame):
        log.info(f"signal {signum} received; finishing the current job then exiting")
        worker.stop()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    worker.run_forever()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NetProvision durable worker")
    parser.add_argument("--config", default=os.getenv("NP_CONFIG", "config.yaml"))
    parser.add_argument("--db", default=os.getenv("NP_DB_PATH", "inventory.db"))
    parser.add_argument("--once", action="store_true",
                        help="drain the queue and exit (cron / smoke tests)")
    parser.add_argument("--collect", action="store_true",
                        help="collect inventory before starting")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument("--poll-seconds", type=float, default=None)
    args = parser.parse_args(argv)

    from observability import configure_logging
    configure_logging(
        os.getenv("NP_LOG_LEVEL", "INFO"),
        os.getenv("NP_LOG_FORMAT", "text"),
    )

    run_worker(
        config_path   = args.config,
        db_path       = args.db,
        once          = args.once,
        collect_first = args.collect,
        worker_id     = args.worker_id,
        lease_seconds = args.lease_seconds,
        poll_seconds  = args.poll_seconds,
    )


if __name__ == "__main__":
    main()
