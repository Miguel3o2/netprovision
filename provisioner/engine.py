"""
Provisioning Engine
===================
Executes VLAN provisioning requests against network devices.

Three phases:
  1. Validate  — check all devices reachable, VLAN ID valid, no conflicts
  2. Plan      — build ordered list of OperationSteps with IOS commands
  3. Execute   — SSH to each device, send commands, record output
  4. Rollback  — on failure, send rollback commands for completed steps

The engine is deterministic: given the same VLANProvisionRequest and
device state, it always produces the same command sequence.

Separation of concerns:
  - build_plan()  is pure — no SSH, fully testable
  - execute()     requires SSH
  - rollback()    requires SSH
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from models import (
    VLANProvisionRequest, ProvisioningJob, OperationStep,
    OperationType, JobStatus, DeviceCredentials,
)
from inventory.store import InventoryStore

log = logging.getLogger(__name__)

try:
    from netmiko import ConnectHandler
    NETMIKO_AVAILABLE = True
except ImportError:
    NETMIKO_AVAILABLE = False


# ── Validation ────────────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


def validate(
    request: VLANProvisionRequest,
    store:   InventoryStore,
    device_map: dict[str, DeviceCredentials],
) -> list[str]:
    """
    Validate a provisioning request.
    Returns list of error strings (empty = valid).
    """
    errors: list[str] = []

    # VLAN ID range
    if not (1 <= request.vlan_id <= 4094):
        errors.append(f"VLAN ID {request.vlan_id} is out of range (1–4094)")

    # Reserved VLANs
    reserved = {1, 1002, 1003, 1004, 1005}
    if request.vlan_id in reserved:
        errors.append(f"VLAN {request.vlan_id} is reserved and cannot be provisioned")

    # VLAN name
    if not request.name or not request.name.strip():
        errors.append("VLAN name cannot be empty")
    elif len(request.name) > 32:
        errors.append("VLAN name exceeds 32 characters (IOS limit)")
    elif " " in request.name:
        errors.append("VLAN name cannot contain spaces (IOS limit)")

    # All referenced devices must be in device_map
    all_devices = set(request.switches)
    for ref in request.add_to_trunks + request.access_ports:
        if ":" in ref:
            all_devices.add(ref.split(":", 1)[0])

    for hostname in all_devices:
        if hostname not in device_map:
            errors.append(f"Device '{hostname}' not found in config")

    # Trunk references must be valid format "hostname:interface"
    for ref in request.add_to_trunks:
        if ":" not in ref:
            errors.append(f"Trunk reference '{ref}' must be 'hostname:interface'")

    # Access port references
    for ref in request.access_ports:
        if ":" not in ref:
            errors.append(f"Access port reference '{ref}' must be 'hostname:interface'")

    # Check inventory for duplicate VLAN on target switches (warn, not error)
    for hostname in request.switches:
        if store.vlan_exists_on_device(hostname, request.vlan_id):
            errors.append(
                f"VLAN {request.vlan_id} already exists on {hostname} — "
                f"remove it from 'switches' list or delete it first"
            )

    return errors


# ── Plan builder ──────────────────────────────────────────────────────────────

def build_plan(request: VLANProvisionRequest) -> list[OperationStep]:
    """
    Build the ordered list of OperationSteps for a provisioning request.
    Pure function — no I/O, fully testable.

    Order:
      1. Create VLAN on each switch in request.switches
      2. Add VLAN to trunk interfaces in request.add_to_trunks
      3. Assign VLAN to access ports in request.access_ports
    """
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
        steps.append(OperationStep(
            device       = hostname,
            operation    = OperationType.TRUNK_ADD_VLAN,
            interface    = iface,
            vlan_id      = vid,
            vlan_name    = name,
            command      = _cmd_trunk_add(iface, vid),
            rollback_cmd = _cmd_trunk_remove(iface, vid),
        ))

    # Step 3: Assign to access ports
    for ref in request.access_ports:
        hostname, iface = ref.split(":", 1)
        steps.append(OperationStep(
            device       = hostname,
            operation    = OperationType.ACCESS_SET_VLAN,
            interface    = iface,
            vlan_id      = vid,
            vlan_name    = name,
            command      = _cmd_access_vlan(iface, vid),
            rollback_cmd = _cmd_access_vlan(iface, 1),  # revert to VLAN 1
        ))

    return steps


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
    return "write memory"


# ── Execution ─────────────────────────────────────────────────────────────────

def _send(conn, command: str) -> str:
    """Send a multi-line config block via Netmiko."""
    lines = command.strip().splitlines()
    if len(lines) == 1:
        return conn.send_command(lines[0], expect_string=r"#")

    # Multi-line: use send_config_set for config blocks
    config_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped in ("configure terminal", "exit", "end"):
            continue
        config_lines.append(stripped)

    output = conn.send_config_set(config_lines)
    return output


def execute(
    job:        ProvisioningJob,
    store:      InventoryStore,
    device_map: dict[str, DeviceCredentials],
) -> ProvisioningJob:
    """
    Execute a provisioning job against real devices.
    Updates job in-place and writes to store after each step.
    """
    if not NETMIKO_AVAILABLE:
        job.status = JobStatus.FAILED
        job.error  = "netmiko not installed"
        store.write_job(job)
        return job

    job.status = JobStatus.RUNNING
    store.write_job(job)

    # Group steps by device to reuse SSH connections
    steps_by_device: dict[str, list[tuple[int, OperationStep]]] = {}
    for idx, step in enumerate(job.steps):
        steps_by_device.setdefault(step.device, []).append((idx, step))

    completed: list[int] = []   # indices of successfully completed steps

    for hostname, indexed_steps in steps_by_device.items():
        creds = device_map.get(hostname)
        if not creds:
            for idx, step in indexed_steps:
                step.success   = False
                step.error     = f"Device '{hostname}' not in config"
                step.timestamp = datetime.now(timezone.utc)
            continue

        try:
            with ConnectHandler(
                device_type  = creds.device_type.value,
                host         = creds.ip,
                username     = creds.username,
                password     = creds.password,
                secret       = creds.enable_secret or "",
                port         = creds.port,
                timeout      = creds.timeout,
                fast_cli     = False,
            ) as conn:
                if creds.enable_secret:
                    conn.enable()

                for idx, step in indexed_steps:
                    step.timestamp = datetime.now(timezone.utc)
                    try:
                        output = _send(conn, step.command)
                        step.output  = output
                        step.success = True
                        completed.append(idx)
                        log.info(f"{hostname}: {step.operation.value} VLAN {step.vlan_id} — OK")
                    except Exception as e:
                        step.success = False
                        step.error   = str(e)
                        log.error(f"{hostname}: {step.operation.value} VLAN {step.vlan_id} — {e}")

                # Save config on success
                if all(step.success for _, step in indexed_steps):
                    try:
                        conn.send_command(_cmd_save(), expect_string=r"#")
                    except Exception:
                        pass

        except Exception as e:
            for idx, step in indexed_steps:
                if step.timestamp is None:
                    step.success   = False
                    step.error     = f"SSH connection failed: {e}"
                    step.timestamp = datetime.now(timezone.utc)
            log.error(f"{hostname}: SSH connection failed — {e}")

    job.completed_at = datetime.now(timezone.utc)

    if all(s.success for s in job.steps):
        job.status = JobStatus.SUCCESS
        # Update inventory with new VLAN
        _update_inventory_after_success(job, store)
    else:
        job.status = JobStatus.FAILED
        job.error  = f"{job.steps_failed} step(s) failed"

    store.write_job(job)
    return job


def rollback(
    job:        ProvisioningJob,
    store:      InventoryStore,
    device_map: dict[str, DeviceCredentials],
) -> ProvisioningJob:
    """
    Roll back all successfully completed steps in reverse order.
    """
    if not NETMIKO_AVAILABLE:
        return job

    completed_steps = [s for s in reversed(job.steps) if s.success and s.rollback_cmd]

    steps_by_device: dict[str, list[OperationStep]] = {}
    for step in completed_steps:
        steps_by_device.setdefault(step.device, []).append(step)

    for hostname, steps in steps_by_device.items():
        creds = device_map.get(hostname)
        if not creds:
            continue
        try:
            with ConnectHandler(
                device_type = creds.device_type.value,
                host        = creds.ip,
                username    = creds.username,
                password    = creds.password,
                secret      = creds.enable_secret or "",
                port        = creds.port,
                timeout     = creds.timeout,
                fast_cli    = False,
            ) as conn:
                if creds.enable_secret:
                    conn.enable()
                for step in steps:
                    try:
                        _send(conn, step.rollback_cmd)
                        log.info(f"{hostname}: rolled back {step.operation.value} VLAN {step.vlan_id}")
                    except Exception as e:
                        log.error(f"{hostname}: rollback failed for {step.operation.value}: {e}")
                conn.send_command(_cmd_save(), expect_string=r"#")
        except Exception as e:
            log.error(f"{hostname}: rollback SSH failed — {e}")

    job.status = JobStatus.ROLLEDBACK
    store.write_job(job)
    return job


def _update_inventory_after_success(job: ProvisioningJob, store: InventoryStore) -> None:
    """Update the local inventory to reflect the provisioned VLAN."""
    from models import VLANRecord, VLANStatus
    now = datetime.now(timezone.utc)
    req = job.request

    for hostname in req.switches:
        store.upsert_vlan(VLANRecord(
            vlan_id    = req.vlan_id,
            name       = req.name,
            status     = VLANStatus.ACTIVE,
            device     = hostname,
            discovered = now,
        ))

    for ref in req.add_to_trunks:
        hostname, iface_name = ref.split(":", 1)
        iface = store.get_interface(hostname, iface_name)
        if iface and req.vlan_id not in iface.trunk_vlans:
            iface.trunk_vlans.append(req.vlan_id)
            iface.trunk_vlans.sort()
            store.upsert_interface(iface)

    for ref in req.access_ports:
        hostname, iface_name = ref.split(":", 1)
        iface = store.get_interface(hostname, iface_name)
        if iface:
            iface.access_vlan = req.vlan_id
            store.upsert_interface(iface)
