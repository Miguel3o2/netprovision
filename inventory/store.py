"""
Inventory Store
==============
SQLite-backed storage for:
  - devices         — hostname, IP, platform, model, serial
  - interfaces      — per-device interface records
  - vlans           — per-device VLAN database
  - provisioning_jobs — full audit trail of every provisioning operation

Schema changes are additive and applied in place by `_migrate()`, so an
existing inventory.db keeps working after an upgrade.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from models import (
    DeviceRecord,
    InterfaceMode,
    InterfaceRecord,
    InterfaceStatus,
    JobStatus,
    OperationStep,
    OperationType,
    ProvisioningJob,
    VLANRecord,
    VLANStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    hostname    TEXT PRIMARY KEY,
    ip          TEXT NOT NULL,
    device_type TEXT NOT NULL,
    platform    TEXT DEFAULT '',
    serial      TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    uptime      TEXT DEFAULT '',
    tags        TEXT DEFAULT '{}',
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS interfaces (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device       TEXT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT DEFAULT '',
    mode         TEXT DEFAULT 'unknown',
    status       TEXT DEFAULT 'unknown',
    access_vlan  INTEGER,
    trunk_vlans  TEXT DEFAULT '[]',
    native_vlan  INTEGER,
    ip_address   TEXT,
    mac_address  TEXT,
    speed        TEXT,
    duplex       TEXT,
    discovered   TEXT,
    UNIQUE(device, name)
);
CREATE INDEX IF NOT EXISTS idx_if_device ON interfaces(device);

CREATE TABLE IF NOT EXISTS vlans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device     TEXT NOT NULL,
    vlan_id    INTEGER NOT NULL,
    name       TEXT DEFAULT '',
    status     TEXT DEFAULT 'active',
    discovered TEXT,
    UNIQUE(device, vlan_id)
);
CREATE INDEX IF NOT EXISTS idx_vlan_device ON vlans(device);

CREATE TABLE IF NOT EXISTS provisioning_jobs (
    job_id       TEXT PRIMARY KEY,
    vlan_id      INTEGER NOT NULL,
    vlan_name    TEXT NOT NULL,
    switches     TEXT NOT NULL,
    add_to_trunks TEXT NOT NULL,
    access_ports TEXT NOT NULL,
    requested_by TEXT DEFAULT 'api',
    status       TEXT NOT NULL,
    steps        TEXT DEFAULT '[]',
    created_at   TEXT,
    completed_at TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_vlan ON provisioning_jobs(vlan_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON provisioning_jobs(created_at);
"""

MIGRATIONS: dict[str, dict[str, str]] = {
    "provisioning_jobs": {
        "idempotency_key":  "TEXT",
        "steps_total":      "INTEGER DEFAULT 0",
        "snapshot":         "TEXT DEFAULT '{}'",
        "verification":     "TEXT DEFAULT '[]'",
        "approved_by":      "TEXT DEFAULT ''",
        "approved_at":      "TEXT",
        "approval_comment": "TEXT DEFAULT ''",
        "lease_owner":      "TEXT",
        "lease_expires_at": "TEXT",
        "attempts":         "INTEGER DEFAULT 0",
        "rollback_requested_at": "TEXT",
        "rollback_requested_by": "TEXT DEFAULT ''",
    },
}


class InventoryStore:
    def __init__(self, path: str = "inventory.db"):
        self.path  = path
        self._lock = threading.Lock()
        self._memory = path == ":memory:"
        self._mem_conn: Optional[sqlite3.Connection] = None
        self._init()

    def _raw_connect(self) -> sqlite3.Connection:
        if self._memory:
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = self._raw_connect()
            try:
                yield conn
                conn.commit()
            finally:
                if not self._memory:
                    conn.close()

    def _init(self) -> None:
        with self._lock:
            conn = self._raw_connect()
            try:
                if not self._memory:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(SCHEMA)
                self._migrate(conn)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
                    "ON provisioning_jobs(idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_status "
                    "ON provisioning_jobs(status)"
                )
                conn.commit()
            finally:
                if not self._memory:
                    conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        with self._lock:
            if self._mem_conn is not None:
                self._mem_conn.close()
                self._mem_conn = None


    def upsert_device(self, device: DeviceRecord) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO devices (hostname, ip, device_type, platform, serial,
                    model, uptime, tags, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(hostname) DO UPDATE SET
                    ip=excluded.ip, device_type=excluded.device_type,
                    platform=excluded.platform, serial=excluded.serial,
                    model=excluded.model, uptime=excluded.uptime,
                    tags=excluded.tags, last_seen=excluded.last_seen
            """, (
                device.hostname, device.ip, device.device_type,
                device.platform, device.serial, device.model, device.uptime,
                json.dumps(device.tags),
                device.last_seen.isoformat() if device.last_seen else None,
            ))

    def get_device(self, hostname: str) -> Optional[DeviceRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM devices WHERE hostname=?", (hostname,)).fetchone()
        if not row:
            return None
        return DeviceRecord(
            hostname    = row["hostname"],
            ip          = row["ip"],
            device_type = row["device_type"],
            platform    = row["platform"],
            serial      = row["serial"],
            model       = row["model"],
            uptime      = row["uptime"],
            tags        = json.loads(row["tags"]),
            last_seen   = datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None,
        )

    def list_devices(self) -> list[DeviceRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT hostname FROM devices ORDER BY hostname").fetchall()
        devices = (self.get_device(row["hostname"]) for row in rows)
        return [device for device in devices if device is not None]


    def upsert_interface(self, iface: InterfaceRecord) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO interfaces
                    (device, name, description, mode, status, access_vlan,
                     trunk_vlans, native_vlan, ip_address, mac_address,
                     speed, duplex, discovered)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(device, name) DO UPDATE SET
                    description=excluded.description, mode=excluded.mode,
                    status=excluded.status, access_vlan=excluded.access_vlan,
                    trunk_vlans=excluded.trunk_vlans, native_vlan=excluded.native_vlan,
                    ip_address=excluded.ip_address, mac_address=excluded.mac_address,
                    speed=excluded.speed, duplex=excluded.duplex,
                    discovered=excluded.discovered
            """, (
                iface.device, iface.name, iface.description,
                iface.mode.value, iface.status.value,
                iface.access_vlan, json.dumps(iface.trunk_vlans),
                iface.native_vlan, iface.ip_address, iface.mac_address,
                iface.speed, iface.duplex,
                iface.discovered.isoformat() if iface.discovered else None,
            ))

    def get_interfaces(self, device: str) -> list[InterfaceRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM interfaces WHERE device=? ORDER BY name", (device,)
            ).fetchall()
        return [_row_to_iface(r) for r in rows]

    def get_interface(self, device: str, name: str) -> Optional[InterfaceRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM interfaces WHERE device=? AND name=?", (device, name)
            ).fetchone()
        return _row_to_iface(row) if row else None

    def find_interfaces_by_vlan(self, vlan_id: int) -> list[InterfaceRecord]:
        """Return all interfaces with this VLAN as access or trunk member."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM interfaces WHERE access_vlan=? OR trunk_vlans LIKE ?",
                (vlan_id, f"%{vlan_id}%"),
            ).fetchall()
        result = []
        for row in rows:
            iface = _row_to_iface(row)
            if iface.access_vlan == vlan_id or vlan_id in iface.trunk_vlans:
                result.append(iface)
        return result


    def upsert_vlan(self, vlan: VLANRecord) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO vlans (device, vlan_id, name, status, discovered)
                VALUES (?,?,?,?,?)
                ON CONFLICT(device, vlan_id) DO UPDATE SET
                    name=excluded.name, status=excluded.status,
                    discovered=excluded.discovered
            """, (
                vlan.device, vlan.vlan_id, vlan.name, vlan.status.value,
                vlan.discovered.isoformat() if vlan.discovered else None,
            ))

    def get_vlans(self, device: Optional[str] = None) -> list[VLANRecord]:
        with self._conn() as conn:
            if device:
                rows = conn.execute(
                    "SELECT * FROM vlans WHERE device=? ORDER BY vlan_id", (device,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM vlans ORDER BY device, vlan_id"
                ).fetchall()
        return [VLANRecord(
            vlan_id    = r["vlan_id"],
            name       = r["name"],
            status     = VLANStatus(r["status"]),
            device     = r["device"],
            discovered = datetime.fromisoformat(r["discovered"]) if r["discovered"] else None,
        ) for r in rows]

    def vlan_exists_on_device(self, device: str, vlan_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM vlans WHERE device=? AND vlan_id=?", (device, vlan_id)
            ).fetchone()
        return row is not None


    def write_job(self, job: ProvisioningJob, idempotency_key: Optional[str] = None) -> None:
        req = job.request
        job.steps_total = len(job.steps)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO provisioning_jobs
                    (job_id, vlan_id, vlan_name, switches, add_to_trunks,
                     access_ports, requested_by, status, steps, steps_total,
                     created_at, completed_at, error, idempotency_key,
                     snapshot, verification, approved_by, approved_at,
                     approval_comment, rollback_requested_at,
                     rollback_requested_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, steps=excluded.steps,
                    steps_total=excluded.steps_total,
                    completed_at=excluded.completed_at, error=excluded.error,
                    snapshot=excluded.snapshot, verification=excluded.verification,
                    approved_by=excluded.approved_by, approved_at=excluded.approved_at,
                    approval_comment=excluded.approval_comment,
                    rollback_requested_at=excluded.rollback_requested_at,
                    rollback_requested_by=excluded.rollback_requested_by
            """, (
                job.job_id, req.vlan_id, req.name,
                json.dumps(req.switches),
                json.dumps(req.add_to_trunks),
                json.dumps(req.access_ports),
                req.requested_by, job.status.value,
                json.dumps([_step_to_dict(s) for s in job.steps]),
                job.steps_total,
                job.created_at.isoformat() if job.created_at else None,
                job.completed_at.isoformat() if job.completed_at else None,
                job.error,
                idempotency_key,
                json.dumps(job.snapshot),
                json.dumps(job.verification),
                job.approved_by,
                job.approved_at.isoformat() if job.approved_at else None,
                job.approval_comment,
                job.rollback_requested_at.isoformat() if job.rollback_requested_at else None,
                job.rollback_requested_by,
            ))

    def get_job(self, job_id: str) -> Optional[ProvisioningJob]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM provisioning_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def get_job_by_idempotency_key(self, key: str) -> Optional[ProvisioningJob]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM provisioning_jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> list[ProvisioningJob]:
        """List jobs without their step payload (call get_job for detail)."""
        query = "SELECT * FROM provisioning_jobs"
        params: list = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(r, with_steps=False) for r in rows]

    def count_jobs(self, status: Optional[str] = None) -> int:
        query = "SELECT COUNT(*) AS n FROM provisioning_jobs"
        params: list = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        with self._conn() as conn:
            return int(conn.execute(query, params).fetchone()["n"])

    def stats(self) -> dict[str, int]:
        tables = ("devices", "interfaces", "vlans", "provisioning_jobs")
        with self._conn() as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
                for table in tables
            }
        return {
            "devices":    counts["devices"],
            "interfaces": counts["interfaces"],
            "vlans":      counts["vlans"],
            "jobs":       counts["provisioning_jobs"],
        }


    def claim_next_job(
        self,
        worker_id: str,
        lease_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> Optional[ProvisioningJob]:
        """
        Atomically take the oldest pending job and lease it to `worker_id`.

        `BEGIN IMMEDIATE` takes SQLite's write lock before the SELECT, so two
        workers — in one process or two — cannot both claim the same row. The
        job is marked RUNNING here; execute() then re-writes the same status.

        Pending approval is a different state and is never claimable.
        """
        claimed_at = now or datetime.now(timezone.utc)
        expires = claimed_at + timedelta(seconds=lease_seconds)
        job_id: Optional[str] = None

        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT job_id FROM provisioning_jobs WHERE status=? "
                "ORDER BY created_at LIMIT 1",
                (JobStatus.PENDING.value,),
            ).fetchone()
            if row is not None:
                job_id = row["job_id"]
                conn.execute(
                    """
                    UPDATE provisioning_jobs
                    SET status=?, lease_owner=?, lease_expires_at=?,
                        attempts=COALESCE(attempts, 0) + 1
                    WHERE job_id=? AND status=?
                    """,
                    (
                        JobStatus.RUNNING.value, worker_id, expires.isoformat(),
                        job_id, JobStatus.PENDING.value,
                    ),
                )

        return self.get_job(job_id) if job_id else None

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> bool:
        """Extend a lease. False means the lease was lost (expired or reassigned)."""
        expires = (now or datetime.now(timezone.utc)) + timedelta(seconds=lease_seconds)
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE provisioning_jobs SET lease_expires_at=?
                WHERE job_id=? AND lease_owner=? AND status=?
                """,
                (
                    expires.isoformat(), job_id, worker_id,
                    JobStatus.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def clear_lease(self, job_id: str, worker_id: Optional[str] = None) -> None:
        query = (
            "UPDATE provisioning_jobs SET lease_owner=NULL, lease_expires_at=NULL "
            "WHERE job_id=?"
        )
        params: list[Any] = [job_id]
        if worker_id is not None:
            query += " AND lease_owner=?"
            params.append(worker_id)
        with self._conn() as conn:
            conn.execute(query, params)

    def expired_leases(
        self,
        now: Optional[datetime] = None,
        exclude_worker: Optional[str] = None,
    ) -> list[ProvisioningJob]:
        """
        Jobs whose worker stopped heartbeating.

        The caller decides what to do; `exclude_worker` protects a still-running
        worker from reclaiming its own job if the clock or a heartbeat hiccups.
        """
        moment = now or datetime.now(timezone.utc)
        query = (
            "SELECT * FROM provisioning_jobs "
            "WHERE status=? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?"
        )
        params: list[Any] = [JobStatus.RUNNING.value, moment.isoformat()]
        if exclude_worker:
            query += " AND (lease_owner IS NULL OR lease_owner != ?)"
            params.append(exclude_worker)
        query += " ORDER BY created_at"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def fail_interrupted_job(
        self,
        job_id: str,
        error: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Close out an interrupted job without touching a device.

        We deliberately do not auto-rollback here: the original worker may still
        be alive (a partition, not a crash), and two writers on one switch is
        worse than a manual review. The stored steps and snapshot make the
        rollback endpoint usable once a human confirms the state.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE provisioning_jobs
                SET status=?, completed_at=?, error=?,
                    lease_owner=NULL, lease_expires_at=NULL
                WHERE job_id=? AND status=?
                """,
                (
                    JobStatus.FAILED.value,
                    (now or datetime.now(timezone.utc)).isoformat(),
                    error, job_id, JobStatus.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1


    def request_rollback(
        self,
        job_id: str,
        requested_by: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Ask a worker to revert a finished job.

        Marking a request is not a claim: the job keeps its current status until
        a worker actually starts reverting it, so the UI shows the truth.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE provisioning_jobs
                SET rollback_requested_at=?, rollback_requested_by=?
                WHERE job_id=? AND status IN (?, ?)
                  AND rollback_requested_at IS NULL
                """,
                (
                    (now or datetime.now(timezone.utc)).isoformat(), requested_by,
                    job_id, JobStatus.FAILED.value, JobStatus.SUCCESS.value,
                ),
            )
        return cursor.rowcount == 1

    def claim_rollback_job(
        self,
        worker_id: str,
        lease_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> Optional[ProvisioningJob]:
        """Atomically lease the oldest requested rollback. Same rules as jobs."""
        moment = now or datetime.now(timezone.utc)
        expires = moment + timedelta(seconds=lease_seconds)
        job_id: Optional[str] = None

        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT job_id FROM provisioning_jobs
                WHERE status IN (?, ?) AND rollback_requested_at IS NOT NULL
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                ORDER BY rollback_requested_at LIMIT 1
                """,
                (JobStatus.FAILED.value, JobStatus.SUCCESS.value, moment.isoformat()),
            ).fetchone()
            if row is not None:
                job_id = row["job_id"]
                conn.execute(
                    """
                    UPDATE provisioning_jobs SET lease_owner=?, lease_expires_at=?
                    WHERE job_id=? AND status IN (?, ?)
                    """,
                    (
                        worker_id, expires.isoformat(), job_id,
                        JobStatus.FAILED.value, JobStatus.SUCCESS.value,
                    ),
                )

        return self.get_job(job_id) if job_id else None

    def expired_rollback_leases(
        self,
        now: Optional[datetime] = None,
        exclude_worker: Optional[str] = None,
    ) -> list[ProvisioningJob]:
        moment = now or datetime.now(timezone.utc)
        query = (
            "SELECT * FROM provisioning_jobs WHERE status IN (?, ?) "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?"
        )
        params: list[Any] = [
            JobStatus.FAILED.value, JobStatus.SUCCESS.value, moment.isoformat(),
        ]
        if exclude_worker:
            query += " AND (lease_owner IS NULL OR lease_owner != ?)"
            params.append(exclude_worker)
        query += " ORDER BY rollback_requested_at"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def release_expired_rollback(
        self,
        job_id: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Drop a stale rollback lease so the next worker pass can retry.

        The expiry check is repeated in the UPDATE so a heartbeat that landed
        between the SELECT and here is not clobbered. A rollback is safe to
        retry: steps already reverted are flagged `rollback_applied`.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE provisioning_jobs
                SET lease_owner=NULL, lease_expires_at=NULL
                WHERE job_id=? AND status IN (?, ?)
                  AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (
                    job_id, JobStatus.FAILED.value, JobStatus.SUCCESS.value,
                    (now or datetime.now(timezone.utc)).isoformat(),
                ),
            )
        return cursor.rowcount == 1


def _row_to_iface(row: sqlite3.Row) -> InterfaceRecord:
    return InterfaceRecord(
        device      = row["device"],
        name        = row["name"],
        description = row["description"],
        mode        = InterfaceMode(row["mode"]),
        status      = InterfaceStatus(row["status"]),
        access_vlan = row["access_vlan"],
        trunk_vlans = json.loads(row["trunk_vlans"]),
        native_vlan = row["native_vlan"],
        ip_address  = row["ip_address"],
        mac_address = row["mac_address"],
        speed       = row["speed"],
        duplex      = row["duplex"],
        discovered  = datetime.fromisoformat(row["discovered"]) if row["discovered"] else None,
    )


def _step_to_dict(step: OperationStep) -> dict:
    return {
        "device":           step.device,
        "operation":        step.operation.value,
        "interface":        step.interface,
        "vlan_id":          step.vlan_id,
        "vlan_name":        step.vlan_name,
        "command":          step.command,
        "output":           step.output,
        "success":          step.success,
        "error":            step.error,
        "timestamp":        step.timestamp.isoformat() if step.timestamp else None,
        "rollback_cmd":     step.rollback_cmd,
        "rollback_note":    step.rollback_note,
        "rollback_applied": step.rollback_applied,
        "rollback_error":   step.rollback_error,
    }


def _row_to_job(row: sqlite3.Row, with_steps: bool = True) -> ProvisioningJob:
    from models import VLANProvisionRequest

    keys = row.keys()
    steps: list[OperationStep] = []
    if with_steps:
        for s in json.loads(row["steps"]):
            steps.append(OperationStep(
                device           = s["device"],
                operation        = OperationType(s["operation"]),
                interface        = s["interface"],
                vlan_id          = s["vlan_id"],
                vlan_name        = s["vlan_name"],
                command          = s["command"],
                output           = s["output"],
                success          = s["success"],
                error            = s["error"],
                timestamp        = datetime.fromisoformat(s["timestamp"]) if s["timestamp"] else None,
                rollback_cmd     = s.get("rollback_cmd", ""),
                rollback_note    = s.get("rollback_note", ""),
                rollback_applied = bool(s.get("rollback_applied", False)),
                rollback_error   = s.get("rollback_error", ""),
            ))

    return ProvisioningJob(
        job_id  = row["job_id"],
        request = VLANProvisionRequest(
            vlan_id       = row["vlan_id"],
            name          = row["vlan_name"],
            switches      = json.loads(row["switches"]),
            add_to_trunks = json.loads(row["add_to_trunks"]),
            access_ports  = json.loads(row["access_ports"]),
            requested_by  = row["requested_by"],
        ),
        status       = JobStatus(row["status"]),
        steps        = steps,
        steps_total  = row["steps_total"] if "steps_total" in keys else len(steps),
        created_at   = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        completed_at = datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        error        = row["error"],
        approved_by      = row["approved_by"] if "approved_by" in keys else "",
        approved_at      = _parse_dt(row["approved_at"]) if "approved_at" in keys else None,
        approval_comment = row["approval_comment"] if "approval_comment" in keys else "",
        snapshot     = (
            _parse_json(row["snapshot"], {})
            if with_steps and "snapshot" in keys
            else {}
        ),
        verification = _parse_json(row["verification"], []) if "verification" in keys else [],
        attempts         = int(row["attempts"]) if "attempts" in keys and row["attempts"] is not None else 0,
        lease_owner      = (row["lease_owner"] or "") if "lease_owner" in keys else "",
        lease_expires_at = _parse_dt(row["lease_expires_at"]) if "lease_expires_at" in keys else None,
        rollback_requested_at = (
            _parse_dt(row["rollback_requested_at"])
            if "rollback_requested_at" in keys else None
        ),
        rollback_requested_by = (
            (row["rollback_requested_by"] or "")
            if "rollback_requested_by" in keys else ""
        ),
    )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _parse_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
