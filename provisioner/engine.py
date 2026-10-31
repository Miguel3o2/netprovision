"""
Provisioning Engine
===================
Executes VLAN provisioning requests against network devices.

Phases:
  1. Validate  — VLAN id/name/device checks against config + inventory
  2. Plan      — build ordered OperationSteps, pure and fully testable
  3. Execute   — SSH to each device in plan order, verify, persist every step
  4. Rollback  — reverse completed steps, restoring captured prior state

Separation of concerns:
  - build_plan()      pure — no I/O, no SSH
  - inspect_request() reads inventory only, produces warnings
  - execute()/rollback() are the only functions that touch the network, through
    transport.connect() so they can be tested with a fake transport.

Concurrency:
  - one lock per device, so two jobs never talk to the same switch at once
  - one claim per (VLAN, device), so two jobs cannot provision the same VLAN on
    the same device concurrently
  Both registries are in-process; a multi-worker deployment needs a shared
  backend (Phase 1: queue + Postgres advisory locks).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import transport
from inventory.store import InventoryStore
from models import (
    DeviceCredentials,
    InterfaceMode,
    InterfaceRecord,
    JobStatus,
    OperationStep,
    OperationType,
    ProvisioningJob,
    VLANProvisionRequest,
)
from observability import METRICS, reset_job_id, set_job_id
from verify import capture_snapshot, verify_request

log = logging.getLogger(__name__)

VLAN_MIN = 2
VLAN_MAX = 4094
RESERVED_VLANS = {1, 1002, 1003, 1004, 1005}
NAME_MAX_LEN = 32
DEVICE_LOCK_TIMEOUT = 300.0


class ValidationError(Exception):
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _split_ref(ref: str) -> Optional[tuple[str, str]]:
    """Split 'hostname:interface' → (hostname, interface), or None if malformed."""
    if ":" not in ref:
        return None
    host, iface = ref.split(":", 1)
    if not host or not iface:
        return None
    return host, iface


def _target_hosts(request: VLANProvisionRequest) -> list[str]:
    hosts = set(request.switches)
    for ref in request.add_to_trunks + request.access_ports:
        parsed = _split_ref(ref)
        if parsed:
            hosts.add(parsed[0])
    return sorted(hosts)


def _append_note(existing: Optional[str], note: str) -> str:
    return f"{existing}; {note}" if existing else note


# ── Validation ────────────────────────────────────────────────────────────────

def validate(
    request:    VLANProvisionRequest,
    store:      InventoryStore,
    device_map: dict[str, DeviceCredentials],
) -> list[str]:
    """
    Validate a provisioning request.
    Returns list of error strings (empty = valid).
    """
    errors: list[str] = []

    # VLAN ID range (1 is the default VLAN, 1002-1005 are reserved by IOS)
    if not (VLAN_MIN <= request.vlan_id <= VLAN_MAX):
        errors.append(
            f"VLAN ID {request.vlan_id} is out of range ({VLAN_MIN}–{VLAN_MAX})"
        )
    if request.vlan_id in RESERVED_VLANS:
        errors.append(f"VLAN {request.vlan_id} is reserved and cannot be provisioned")

    # VLAN name
    if not request.name or not request.name.strip():
        errors.append("VLAN name cannot be empty")
    elif len(request.name) > NAME_MAX_LEN:
        errors.append(f"VLAN name exceeds {NAME_MAX_LEN} characters (IOS limit)")
    elif " " in request.name:
        errors.append("VLAN name cannot contain spaces (IOS limit)")

    # At least one target
    if not (request.switches or request.add_to_trunks or request.access_ports):
        errors.append("Request has no target devices, trunks or access ports")

    # All referenced devices must be in device_map
    all_devices = set(request.switches)
    for ref in request.add_to_trunks + request.access_ports:
        parsed = _split_ref(ref)
        if parsed:
            all_devices.add(parsed[0])

    for hostname in sorted(all_devices):
        if hostname not in device_map:
            errors.append(f"Device '{hostname}' not found in config")

    # References must be valid "hostname:interface"
    for ref in request.add_to_trunks:
        if not _split_ref(ref):
            errors.append(f"Trunk reference '{ref}' must be 'hostname:interface'")
    for ref in request.access_ports:
        if not _split_ref(ref):
            errors.append(f"Access port reference '{ref}' must be 'hostname:interface'")

    # Duplicate VLAN check on switches we would create it on
    for hostname in request.switches:
        if store.vlan_exists_on_device(hostname, request.vlan_id):
            errors.append(
                f"VLAN {request.vlan_id} already exists on {hostname} — "
                f"remove it from 'switches' list or delete it first"
            )

    # A trunk / access host that is not in 'switches' must already have the VLAN,
    # otherwise IOS rejects the interface command.
    for ref in request.add_to_trunks + request.access_ports:
        parsed = _split_ref(ref)
        if not parsed:
            continue
        host = parsed[0]
        if host in request.switches:
            continue
        if not store.vlan_exists_on_device(host, request.vlan_id):
            errors.append(
                f"VLAN {request.vlan_id} does not exist on '{host}' — add it to "
                f"'switches' so it is created before '{ref}' is configured"
            )

    return errors


# ── Plan builder ──────────────────────────────────────────────────────────────

def build_plan(
    request: VLANProvisionRequest,
    prior:   Optional[dict[tuple[str, str], InterfaceRecord]] = None,
) -> list[OperationStep]:
    """
    Build the ordered list of OperationSteps.

    Pure function — `prior` is an optional snapshot of current interface state
    (hostname, interface) → InterfaceRecord, normally from the inventory. It is
    used to compute honest rollback commands; without it, steps that cannot be
    reverted safely get an empty rollback_cmd plus a rollback_note.

    Order:
      1. Create VLAN on each switch in request.switches
      2. Add VLAN to trunk interfaces in request.add_to_trunks
      3. Assign VLAN to access ports in request.access_ports
    """
    prior = prior or {}
    steps: list[OperationStep] = []
    vid  = request.vlan_id
    name = request.name

    # Step 1: Create VLAN on switches
    for hostname in request.switches:
        steps.append(OperationStep(
            device       = hostname,
            operation    = OperationType.VLAN_CREATE,
            interface    = None,
            vlan_id      = vid,
            vlan_name    = name,
            command      = _cmd_create_vlan(vid, name),
            rollback_cmd = _cmd_delete_vlan(vid),
        ))

    # Step 2: Add to trunk interfaces
    for ref in request.add_to_trunks:
        hostname, iface = ref.split(":", 1)
        current = prior.get((hostname, iface))
        rollback_cmd, note = _trunk_rollback(iface, vid, current)
        steps.append(OperationStep(
            device       = hostname,
            operation    = OperationType.TRUNK_ADD_VLAN,
            interface    = iface,
            vlan_id      = vid,
            vlan_name    = name,
            command      = _cmd_trunk_add(iface, vid),
            rollback_cmd = rollback_cmd,
            rollback_note= note,
        ))

    # Step 3: Assign to access ports
    for ref in request.access_ports:
        hostname, iface = ref.split(":", 1)
        current = prior.get((hostname, iface))
        rollback_cmd, note = _access_rollback(iface, vid, current)
        steps.append(OperationStep(
            device       = hostname,
            operation    = OperationType.ACCESS_SET_VLAN,
            interface    = iface,
            vlan_id      = vid,
            vlan_name    = name,
            command      = _cmd_access_vlan(iface, vid),
            rollback_cmd = rollback_cmd,
            rollback_note= note,
        ))

    return steps


def _trunk_rollback(
    iface:   str,
    vlan_id: int,
    current: Optional[InterfaceRecord],
) -> tuple[str, str]:
    if current is not None and vlan_id in current.trunk_vlans:
        return (
            "",
            f"VLAN {vlan_id} was already allowed on {iface} before this job — "
            f"rollback is manual so pre-existing config is not removed",
        )
    return _cmd_trunk_remove(iface, vlan_id), ""


def _access_rollback(
    iface:   str,
    vlan_id: int,
    current: Optional[InterfaceRecord],
) -> tuple[str, str]:
    if current is None:
        return (
            "",
            f"previous access VLAN on {iface} is unknown — port is left on VLAN "
            f"{vlan_id}; manual revert required",
        )
    if current.access_vlan == vlan_id:
        return "", f"{iface} already carried VLAN {vlan_id}; nothing to revert"
    if current.access_vlan is not None:
        return _cmd_access_vlan(iface, current.access_vlan), ""
    # No explicit access VLAN in the inventory → the device default is VLAN 1.
    return _cmd_access_vlan(iface, 1), ""


def load_prior_state(
    request: VLANProvisionRequest,
    store:   InventoryStore,
) -> dict[tuple[str, str], InterfaceRecord]:
    """Snapshot current interface state for every interface the plan touches."""
    prior: dict[tuple[str, str], InterfaceRecord] = {}
    for ref in request.add_to_trunks + request.access_ports:
        parsed = _split_ref(ref)
        if not parsed:
            continue
        host, iface = parsed
        record = store.get_interface(host, iface)
        if record is not None:
            prior[(host, iface)] = record
    return prior


def inspect_request(
    request: VLANProvisionRequest,
    store:   InventoryStore,
) -> tuple[list[OperationStep], list[str]]:
    """
    Build the plan plus human-readable warnings about stale or surprising
    inventory state. Reads SQLite only — never touches a device.
    """
    warnings: list[str] = []
    prior = load_prior_state(request, store)

    for ref in request.add_to_trunks:
        parsed = _split_ref(ref)
        if not parsed:
            continue
        host, iface = parsed
        record = prior.get((host, iface))
        if record is None:
            warnings.append(
                f"{host}:{iface} is not in the inventory — trunk state is unknown; "
                f"refresh the device before relying on rollback"
            )
            continue
        if record.mode is InterfaceMode.ACCESS:
            warnings.append(
                f"{host}:{iface} is an access port in the inventory — "
                f"'switchport trunk allowed vlan' may be rejected by the device"
            )
        if request.vlan_id in record.trunk_vlans:
            warnings.append(
                f"{host}:{iface} already allows VLAN {request.vlan_id} — "
                f"this step is a no-op and will not be rolled back"
            )

    for ref in request.access_ports:
        parsed = _split_ref(ref)
        if not parsed:
            continue
        host, iface = parsed
        record = prior.get((host, iface))
        if record is None:
            warnings.append(
                f"{host}:{iface} is not in the inventory — the previous access "
                f"VLAN is unknown, so this step cannot be rolled back automatically"
            )
            continue
        if record.mode is InterfaceMode.TRUNK:
            warnings.append(
                f"{host}:{iface} is a trunk port — converting it to access mode "
                f"removes its trunk configuration"
            )
        if record.access_vlan == request.vlan_id:
            warnings.append(
                f"{host}:{iface} already carries VLAN {request.vlan_id} — "
                f"this step is a no-op"
            )

    steps = build_plan(request, prior)
    return steps, warnings


# ── IOS command builders ──────────────────────────────────────────────────────

def _cmd_create_vlan(vlan_id: int, name: str) -> str:
    return (
        f"configure terminal\n"
        f"vlan {vlan_id}\n"
        f" name {name}\n"
        f"exit\n"
        f"exit"
    )


def _cmd_delete_vlan(vlan_id: int) -> str:
    return (
        f"configure terminal\n"
        f"no vlan {vlan_id}\n"
        f"exit"
    )


def _cmd_trunk_add(interface: str, vlan_id: int) -> str:
    return (
        f"configure terminal\n"
        f"interface {interface}\n"
        f" switchport trunk allowed vlan add {vlan_id}\n"
        f"exit\n"
        f"exit"
    )


def _cmd_trunk_remove(interface: str, vlan_id: int) -> str:
    return (
        f"configure terminal\n"
        f"interface {interface}\n"
        f" switchport trunk allowed vlan remove {vlan_id}\n"
        f"exit\n"
        f"exit"
    )


def _cmd_access_vlan(interface: str, vlan_id: int) -> str:
    return (
        f"configure terminal\n"
        f"interface {interface}\n"
        f" switchport mode access\n"
        f" switchport access vlan {vlan_id}\n"
        f"exit\n"
        f"exit"
    )


def _cmd_save() -> str:
    # IOS/IOS-XE. NX-OS needs 'copy running-config startup-config' (Phase 1:
    # vendor capability model).
    return "write memory"


# ── Concurrency primitives ────────────────────────────────────────────────────

class DeviceLocks:
    """One mutex per device, so concurrent jobs serialise per switch."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    @contextmanager
    def hold(self, host: str, timeout: float = DEVICE_LOCK_TIMEOUT):
        lock = self._lock_for(host)
        if not lock.acquire(timeout=timeout):
            raise TimeoutError(f"timed out waiting for the lock on '{host}'")
        try:
            yield
        finally:
            lock.release()


class VlanClaims:
    """All-or-nothing claims on (VLAN, device), to stop duplicate jobs racing."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: dict[tuple[int, str], str] = {}

    def claim(self, job: ProvisioningJob) -> list[str]:
        conflicts: list[str] = []
        with self._guard:
            for host in _target_hosts(job.request):
                owner = self._active.get((job.request.vlan_id, host))
                if owner and owner != job.job_id:
                    conflicts.append(
                        f"VLAN {job.request.vlan_id} on {host} (job {owner[:8]})"
                    )
            if conflicts:
                return conflicts
            for host in _target_hosts(job.request):
                self._active[(job.request.vlan_id, host)] = job.job_id
        return []

    def release(self, job: ProvisioningJob) -> None:
        with self._guard:
            for key, owner in list(self._active.items()):
                if owner == job.job_id:
                    del self._active[key]


DEVICE_LOCKS = DeviceLocks()
VLAN_CLAIMS = VlanClaims()


# ── Transport helper ──────────────────────────────────────────────────────────

class _Connections:
    """Lazily-opened SSH sessions, reused across a job, closed once."""

    def __init__(self, device_map: dict[str, DeviceCredentials]) -> None:
        self._device_map = device_map
        self._conns: dict[str, Any] = {}

    def get(self, host: str) -> Any:
        if host not in self._conns:
            creds = self._device_map.get(host)
            if creds is None:
                raise ValidationError(f"Device '{host}' not in config")
            self._conns[host] = transport.connect(creds)
        return self._conns[host]

    def save_all(self, locks: DeviceLocks) -> None:
        for host, conn in self._conns.items():
            try:
                with locks.hold(host):
                    conn.send_command(_cmd_save(), expect_string=r"#")
            except Exception as exc:  # saving is best-effort, never fails a job
                log.warning(f"{host}: could not save configuration — {exc}")

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.disconnect()
            except Exception:  # pragma: no cover - teardown best effort
                pass
        self._conns.clear()


def _send(conn, command: str) -> str:
    """
    Send a plan command.

    Single commands go through send_command; multi-line blocks go through
    send_config_set, which enters and leaves config mode itself. Only the
    leading 'configure terminal' and trailing 'exit'/'end' are stripped —
    internal context boundaries are preserved.
    """
    lines = [line.strip() for line in command.strip().splitlines() if line.strip()]
    while lines and lines[0] in ("configure terminal", "conf t"):
        lines.pop(0)
    while lines and lines[-1] in ("exit", "end"):
        lines.pop()

    if not lines:
        return ""
    if len(lines) == 1:
        return conn.send_command(lines[0], expect_string=r"#")
    return conn.send_config_set(lines)


# ── Execution ─────────────────────────────────────────────────────────────────

def execute(
    job:           ProvisioningJob,
    store:         InventoryStore,
    device_map:    dict[str, DeviceCredentials],
    *,
    auto_rollback: bool = True,
    verify_after_apply: bool = True,
    capture_pre_state:  bool = True,
    locks:         Optional[DeviceLocks] = None,
    claims:        Optional[VlanClaims] = None,
) -> ProvisioningJob:
    """
    Execute a provisioning job against real devices.

    Steps run in plan order (VLAN creation everywhere before any trunk/access
    change). Every step is persisted as it completes. The touched objects are
    snapshotted first, the resulting state is verified afterwards, and completed
    steps are rolled back on failure unless `auto_rollback=False`.
    """
    locks  = locks or DEVICE_LOCKS
    claims = claims or VLAN_CLAIMS
    token  = set_job_id(job.job_id)
    started = time.monotonic()

    try:
        # ── Preconditions ────────────────────────────────────────────────────
        if not transport.NETMIKO_AVAILABLE:
            return _finish(job, store, JobStatus.FAILED, "netmiko not installed")

        conflicts = claims.claim(job)
        if conflicts:
            return _finish(
                job, store, JobStatus.FAILED,
                "conflicting job already provisioning: " + "; ".join(conflicts),
            )

        METRICS.inc("np_jobs_in_flight")
        job.status = JobStatus.RUNNING
        store.write_job(job)

        connections = _Connections(device_map)

        def send(host: str, command: str) -> str:
            with locks.hold(host):
                return connections.get(host).send_command(command, expect_string=r"#")

        try:
            if capture_pre_state:
                job.snapshot = capture_snapshot(job.request, send)
                store.write_job(job)

            for number, step in enumerate(job.steps, start=1):
                if step.device not in device_map:
                    step.timestamp = _now()
                    step.success   = False
                    step.error     = f"Device '{step.device}' not in config"
                    METRICS.inc("np_steps_total", {"result": "failed"})
                    job.error = step.error
                    break

                step.timestamp = _now()
                try:
                    with locks.hold(step.device):
                        output = _send(connections.get(step.device), step.command)
                    step.success = True
                    step.output  = output
                    METRICS.inc("np_steps_total", {"result": "ok"})
                    log.info(
                        f"{step.device}: {step.operation.value} VLAN {step.vlan_id} — OK"
                    )
                except Exception as exc:
                    step.success = False
                    step.error   = str(exc)
                    METRICS.inc("np_steps_total", {"result": "failed"})
                    job.error = (
                        f"step {number}/{len(job.steps)} "
                        f"({step.operation.value} on {step.device}) failed: {exc}"
                    )
                    log.error(f"{step.device}: {step.operation.value} — {exc}")
                    break
                finally:
                    store.write_job(job)

            if job.steps and all(s.success for s in job.steps):
                failed = _verify_and_record(job, send) if verify_after_apply else []
                if failed:
                    job.status       = JobStatus.FAILED
                    job.completed_at = _now()
                    details = "; ".join(
                        f"{check['check']} on {check['device']}: "
                        f"expected {check['expected']}, got {check['actual']}"
                        for check in failed
                    )
                    job.error = f"post-change verification failed: {details}"
                    log.error(f"{job.job_id}: {job.error}")
                else:
                    connections.save_all(locks)
                    job.status       = JobStatus.SUCCESS
                    job.completed_at = _now()
                    _update_inventory_after_success(job, store)
            else:
                job.status = JobStatus.FAILED
                job.completed_at = _now()
        finally:
            connections.close()

        if job.status is JobStatus.FAILED and auto_rollback and job.steps_succeeded:
            log.warning(f"{job.job_id}: rolling back {job.steps_succeeded} completed step(s)")
            return rollback(job, store, device_map, locks=locks)

        store.write_job(job)
        log.info(
            f"{job.job_id}: {job.status.value} in {time.monotonic() - started:.1f}s "
            f"({job.steps_succeeded}/{len(job.steps)} steps, "
            f"{len(job.verification)} checks)"
        )
        return job

    except Exception as exc:  # unexpected error must still close out the job
        log.exception(f"{job.job_id}: unhandled execution error")
        job.status       = JobStatus.FAILED
        job.error        = _append_note(job.error, f"internal error: {exc}")
        job.completed_at = _now()
        store.write_job(job)
        return job
    finally:
        claims.release(job)
        METRICS.dec("np_jobs_in_flight")
        METRICS.inc("np_jobs_total", {"status": job.status.value})
        reset_job_id(token)


def _finish(
    job:    ProvisioningJob,
    store:  InventoryStore,
    status: JobStatus,
    error:  Optional[str] = None,
) -> ProvisioningJob:
    job.status       = status
    job.error        = error
    job.completed_at = _now()
    store.write_job(job)
    return job


def _verify_and_record(job: ProvisioningJob, send) -> list[dict]:
    """Run post-change checks, persist them on the job, return the failures."""
    checks = [check.as_dict() for check in verify_request(job.request, send)]
    job.verification = checks
    passed = sum(1 for check in checks if check["ok"])
    log.info(f"{job.job_id}: {passed}/{len(checks)} verification checks passed")
    return [check for check in checks if not check["ok"]]


def rollback(
    job:        ProvisioningJob,
    store:      InventoryStore,
    device_map: dict[str, DeviceCredentials],
    *,
    locks:      Optional[DeviceLocks] = None,
) -> ProvisioningJob:
    """
    Revert every successfully applied step, newest first.

    Steps without a rollback command (unknown prior state, or pre-existing
    config that a revert would damage) are skipped and flagged in
    `rollback_note` for manual follow-up.
    """
    locks = locks or DEVICE_LOCKS
    token = set_job_id(job.job_id)

    try:
        pending = [s for s in reversed(job.steps) if s.success and not s.rollback_applied]
        for step in pending:
            if not step.rollback_cmd:
                step.rollback_note = _append_note(
                    step.rollback_note, "no automated rollback — manual revert required"
                )

        connections = _Connections(device_map)
        try:
            for step in pending:
                if not step.rollback_cmd:
                    log.warning(
                        f"{job.job_id}: manual revert needed for "
                        f"{step.operation.value} on {step.device}"
                    )
                    continue
                try:
                    with locks.hold(step.device):
                        _send(connections.get(step.device), step.rollback_cmd)
                    step.rollback_applied = True
                    METRICS.inc("np_rollback_total", {"result": "ok"})
                    log.info(
                        f"{step.device}: rolled back {step.operation.value} "
                        f"VLAN {step.vlan_id}"
                    )
                except Exception as exc:
                    step.rollback_error = str(exc)
                    METRICS.inc("np_rollback_total", {"result": "failed"})
                    job.error = _append_note(
                        job.error,
                        f"rollback failed for {step.operation.value} on "
                        f"{step.device}: {exc}",
                    )
                    log.error(f"{step.device}: rollback failed — {exc}")
            connections.save_all(locks)
        finally:
            connections.close()

        job.status       = JobStatus.ROLLEDBACK
        job.completed_at = _now()
        store.write_job(job)
        return job
    finally:
        reset_job_id(token)


def _update_inventory_after_success(
    job:   ProvisioningJob,
    store: InventoryStore,
) -> None:
    """Update the local inventory to reflect the provisioned VLAN."""
    from models import VLANRecord, VLANStatus

    now = _now()
    request = job.request

    for hostname in _target_hosts(request):
        store.upsert_vlan(VLANRecord(
            vlan_id    = request.vlan_id,
            name       = request.name,
            status     = VLANStatus.ACTIVE,
            device     = hostname,
            discovered = now,
        ))

    for ref in request.add_to_trunks:
        parsed = _split_ref(ref)
        if not parsed:
            continue
        hostname, iface_name = parsed
        iface = store.get_interface(hostname, iface_name) or InterfaceRecord(
            device=hostname, name=iface_name, discovered=now,
        )
        if request.vlan_id not in iface.trunk_vlans:
            iface.trunk_vlans.append(request.vlan_id)
            iface.trunk_vlans.sort()
        iface.mode = InterfaceMode.TRUNK
        store.upsert_interface(iface)

    for ref in request.access_ports:
        parsed = _split_ref(ref)
        if not parsed:
            continue
        hostname, iface_name = parsed
        iface = store.get_interface(hostname, iface_name) or InterfaceRecord(
            device=hostname, name=iface_name, discovered=now,
        )
        iface.access_vlan = request.vlan_id
        iface.mode        = InterfaceMode.ACCESS
        store.upsert_interface(iface)
