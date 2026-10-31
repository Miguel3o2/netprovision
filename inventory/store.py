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
from datetime import datetime
from typing import Optional

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

# Columns added after the first release. Kept additive so old databases migrate.
MIGRATIONS: dict[str, dict[str, str]] = {
    "provisioning_jobs": {
        "idempotency_key":  "TEXT",
        "steps_total":      "INTEGER DEFAULT 0",
        "snapshot":         "TEXT DEFAULT '{}'",
        "verification":     "TEXT DEFAULT '[]'",
        "approved_by":      "TEXT DEFAULT ''",
        "approved_at":      "TEXT",
        "approval_comment": "TEXT DEFAULT ''",
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
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
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
                conn.executescript(SCHEMA)
                self._migrate(conn)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
                    "ON provisioning_jobs(idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
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

    # ── Devices ───────────────────────────────────────────────────────────────

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

    # ── Interfaces ────────────────────────────────────────────────────────────

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

    # ── VLANs ─────────────────────────────────────────────────────────────────

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

    # ── Provisioning jobs ─────────────────────────────────────────────────────

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
                     approval_comment)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, steps=excluded.steps,
                    steps_total=excluded.steps_total,
                    completed_at=excluded.completed_at, error=excluded.error,
                    snapshot=excluded.snapshot, verification=excluded.verification,
                    approved_by=excluded.approved_by, approved_at=excluded.approved_at,
                    approval_comment=excluded.approval_comment
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


# ── Row converters ────────────────────────────────────────────────────────────

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
    )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _parse_json(value: Optional[str], default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
