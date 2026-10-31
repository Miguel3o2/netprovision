"""
NetProvision tests.
All tests run without SSH — engine logic, validation, and store are fully testable.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone

from inventory.store import InventoryStore
from models import (
    DeviceCredentials,
    DeviceRecord,
    DeviceType,
    InterfaceMode,
    InterfaceRecord,
    InterfaceStatus,
    OperationType,
    VLANProvisionRequest,
    VLANRecord,
)
from provisioner.engine import build_plan, validate


def _store():
    return InventoryStore(path=":memory:")


def _device_map():
    return {
        "SW-CORE": DeviceCredentials(
            hostname="SW-CORE", ip="192.168.10.2",
            device_type=DeviceType.CISCO_IOS,
            username="admin", password="cisco123",
        ),
        "SW-ACCESS": DeviceCredentials(
            hostname="SW-ACCESS", ip="192.168.10.3",
            device_type=DeviceType.CISCO_IOS,
            username="admin", password="cisco123",
        ),
    }


def _req(**kwargs):
    defaults = dict(
        vlan_id=40, name="Finance",
        switches=["SW-CORE", "SW-ACCESS"],
        add_to_trunks=["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
        access_ports=["SW-ACCESS:Fa0/5"],
    )
    defaults.update(kwargs)
    return VLANProvisionRequest(**defaults)


# ── Validation ────────────────────────────────────────────────────────────────

def test_valid_request_has_no_errors():
    errors = validate(_req(), _store(), _device_map())
    assert errors == []


def test_vlan_id_out_of_range():
    errors = validate(_req(vlan_id=4095), _store(), _device_map())
    assert any("out of range" in e for e in errors)


def test_vlan_id_zero():
    errors = validate(_req(vlan_id=0), _store(), _device_map())
    assert any("out of range" in e for e in errors)


def test_reserved_vlan_rejected():
    for vid in [1, 1002, 1003, 1004, 1005]:
        errors = validate(_req(vlan_id=vid), _store(), _device_map())
        assert any("reserved" in e for e in errors), f"VLAN {vid} should be rejected"


def test_empty_vlan_name_rejected():
    errors = validate(_req(name=""), _store(), _device_map())
    assert any("name" in e.lower() for e in errors)


def test_vlan_name_with_spaces_rejected():
    errors = validate(_req(name="my vlan"), _store(), _device_map())
    assert any("spaces" in e for e in errors)


def test_vlan_name_too_long():
    errors = validate(_req(name="x" * 33), _store(), _device_map())
    assert any("32" in e for e in errors)


def test_unknown_device_rejected():
    errors = validate(_req(switches=["UNKNOWN-SW"]), _store(), _device_map())
    assert any("UNKNOWN-SW" in e for e in errors)


def test_bad_trunk_format_rejected():
    errors = validate(_req(add_to_trunks=["SW-COREGi0/1"]), _store(), _device_map())
    assert any("hostname:interface" in e for e in errors)


def test_bad_access_format_rejected():
    errors = validate(_req(access_ports=["SW-ACCESSFa0/5"]), _store(), _device_map())
    assert any("hostname:interface" in e for e in errors)


def test_duplicate_vlan_rejected():
    store = _store()
    store.upsert_vlan(VLANRecord(vlan_id=40, name="Finance", device="SW-CORE"))
    errors = validate(_req(), store, _device_map())
    assert any("already exists" in e for e in errors)


def test_request_with_no_targets_rejected():
    errors = validate(
        _req(switches=[], add_to_trunks=[], access_ports=[]),
        _store(), _device_map(),
    )
    assert any("no target" in e.lower() for e in errors)


def test_trunk_host_without_vlan_is_rejected():
    # SW-ACCESS is not in 'switches', so it must already carry the VLAN.
    errors = validate(
        _req(switches=["SW-CORE"], access_ports=["SW-ACCESS:Fa0/5"]),
        _store(), _device_map(),
    )
    assert any("does not exist on 'SW-ACCESS'" in e for e in errors)


def test_trunk_host_with_existing_vlan_is_allowed():
    store = _store()
    store.upsert_vlan(VLANRecord(vlan_id=40, name="Finance", device="SW-ACCESS"))
    errors = validate(
        _req(switches=["SW-CORE"], access_ports=["SW-ACCESS:Fa0/5"]),
        store, _device_map(),
    )
    assert errors == []


def test_vlan_4094_is_in_range():
    errors = validate(_req(vlan_id=4094), _store(), _device_map())
    assert not any("out of range" in e for e in errors)


# ── Plan builder ──────────────────────────────────────────────────────────────

def test_plan_has_correct_step_count():
    # 2 switches + 2 trunks + 1 access = 5 steps
    steps = build_plan(_req())
    assert len(steps) == 5


def test_plan_creates_vlan_first():
    steps = build_plan(_req())
    create_steps = [s for s in steps if s.operation == OperationType.VLAN_CREATE]
    trunk_steps  = [s for s in steps if s.operation == OperationType.TRUNK_ADD_VLAN]

    # All create steps come before trunk and access steps
    last_create = max(steps.index(s) for s in create_steps)
    first_trunk  = min(steps.index(s) for s in trunk_steps) if trunk_steps else 999
    assert last_create < first_trunk


def test_plan_vlan_create_targets_all_switches():
    req   = _req(switches=["SW-CORE", "SW-ACCESS"])
    steps = build_plan(req)
    create_devices = {s.device for s in steps if s.operation == OperationType.VLAN_CREATE}
    assert create_devices == {"SW-CORE", "SW-ACCESS"}


def test_plan_trunk_add_targets_correct_interfaces():
    req   = _req(add_to_trunks=["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"])
    steps = build_plan(req)
    trunk_steps = [s for s in steps if s.operation == OperationType.TRUNK_ADD_VLAN]
    assert len(trunk_steps) == 2
    assert any(s.interface == "Gi0/1" and s.device == "SW-CORE"   for s in trunk_steps)
    assert any(s.interface == "Gi0/1" and s.device == "SW-ACCESS" for s in trunk_steps)


def test_plan_access_port_targets_correct_interface():
    steps = build_plan(_req())
    access = [s for s in steps if s.operation == OperationType.ACCESS_SET_VLAN]
    assert len(access) == 1
    assert access[0].interface == "Fa0/5"
    assert access[0].device    == "SW-ACCESS"


def test_plan_commands_contain_vlan_id():
    steps = build_plan(_req(vlan_id=40))
    for step in steps:
        assert "40" in step.command, f"Command missing VLAN ID: {step.command}"


def test_plan_rollback_commands_present():
    prior = {
        ("SW-CORE", "Gi0/1"): InterfaceRecord(
            device="SW-CORE", name="Gi0/1", mode=InterfaceMode.TRUNK,
            trunk_vlans=[10, 20],
        ),
        ("SW-ACCESS", "Gi0/1"): InterfaceRecord(
            device="SW-ACCESS", name="Gi0/1", mode=InterfaceMode.TRUNK,
            trunk_vlans=[10, 20],
        ),
        ("SW-ACCESS", "Fa0/5"): InterfaceRecord(
            device="SW-ACCESS", name="Fa0/5", mode=InterfaceMode.ACCESS,
            access_vlan=20,
        ),
    }
    steps = build_plan(_req(), prior)
    for step in steps:
        assert step.rollback_cmd, f"Step {step.operation} missing rollback command"


def test_plan_access_rollback_restores_previous_vlan():
    prior = {
        ("SW-ACCESS", "Fa0/5"): InterfaceRecord(
            device="SW-ACCESS", name="Fa0/5", mode=InterfaceMode.ACCESS,
            access_vlan=20,
        ),
    }
    steps = build_plan(_req(), prior)
    access = next(s for s in steps if s.operation == OperationType.ACCESS_SET_VLAN)
    assert "switchport access vlan 20" in access.rollback_cmd
    assert "switchport access vlan 1" not in access.rollback_cmd


def test_plan_access_rollback_unknown_is_manual():
    steps = build_plan(_req())
    access = next(s for s in steps if s.operation == OperationType.ACCESS_SET_VLAN)
    assert access.rollback_cmd == ""
    assert "manual" in access.rollback_note.lower()


def test_plan_trunk_rollback_skipped_when_vlan_already_allowed():
    prior = {
        ("SW-CORE", "Gi0/1"): InterfaceRecord(
            device="SW-CORE", name="Gi0/1", mode=InterfaceMode.TRUNK,
            trunk_vlans=[40],
        ),
    }
    steps = build_plan(_req(add_to_trunks=["SW-CORE:Gi0/1"]), prior)
    trunk = next(s for s in steps if s.operation == OperationType.TRUNK_ADD_VLAN)
    assert trunk.rollback_cmd == ""
    assert "already allowed" in trunk.rollback_note


def test_plan_trunk_rollback_is_remove():
    steps = build_plan(_req())
    trunk = next(s for s in steps if s.operation == OperationType.TRUNK_ADD_VLAN)
    assert "remove" in trunk.rollback_cmd


def test_plan_vlan_create_rollback_is_no_vlan():
    steps = build_plan(_req())
    create = next(s for s in steps if s.operation == OperationType.VLAN_CREATE)
    assert "no vlan" in create.rollback_cmd


def test_plan_no_switches_no_create_steps():
    req   = _req(switches=[], add_to_trunks=[], access_ports=["SW-ACCESS:Fa0/5"])
    steps = build_plan(req)
    create_steps = [s for s in steps if s.operation == OperationType.VLAN_CREATE]
    assert len(create_steps) == 0
    assert len(steps) == 1


# ── Inventory store ───────────────────────────────────────────────────────────

def test_store_upsert_and_get_device():
    store = _store()
    now   = datetime.now(timezone.utc)
    store.upsert_device(DeviceRecord(
        hostname="R1", ip="192.168.1.1", device_type="cisco_ios",
        model="ISR4321", last_seen=now,
    ))
    device = store.get_device("R1")
    assert device is not None
    assert device.ip    == "192.168.1.1"
    assert device.model == "ISR4321"


def test_store_upsert_updates_existing():
    store = _store()
    store.upsert_device(DeviceRecord(hostname="R1", ip="1.2.3.4", device_type="cisco_ios"))
    store.upsert_device(DeviceRecord(hostname="R1", ip="5.6.7.8", device_type="cisco_ios"))
    assert store.get_device("R1").ip == "5.6.7.8"


def test_store_list_devices():
    store = _store()
    for h in ["R1", "SW-CORE", "SW-ACCESS"]:
        store.upsert_device(DeviceRecord(hostname=h, ip="1.1.1.1", device_type="cisco_ios"))
    devices = store.list_devices()
    hostnames = {d.hostname for d in devices}
    assert {"R1", "SW-CORE", "SW-ACCESS"} == hostnames


def test_store_interface_upsert_and_query():
    store = _store()
    store.upsert_interface(InterfaceRecord(
        device="SW-CORE", name="Gi0/1",
        mode=InterfaceMode.TRUNK,
        status=InterfaceStatus.UP,
        trunk_vlans=[10, 20, 30],
    ))
    ifaces = store.get_interfaces("SW-CORE")
    assert len(ifaces) == 1
    assert ifaces[0].trunk_vlans == [10, 20, 30]
    assert ifaces[0].mode == InterfaceMode.TRUNK


def test_store_find_interfaces_by_vlan():
    store = _store()
    store.upsert_interface(InterfaceRecord(
        device="SW-ACCESS", name="Fa0/1",
        mode=InterfaceMode.ACCESS,
        status=InterfaceStatus.UP,
        access_vlan=20,
    ))
    store.upsert_interface(InterfaceRecord(
        device="SW-CORE", name="Gi0/1",
        mode=InterfaceMode.TRUNK,
        status=InterfaceStatus.UP,
        trunk_vlans=[10, 20, 30],
    ))
    ifaces = store.find_interfaces_by_vlan(20)
    names  = {i.name for i in ifaces}
    assert "Fa0/1" in names
    assert "Gi0/1" in names


def test_store_vlan_upsert_and_query():
    store = _store()
    store.upsert_vlan(VLANRecord(vlan_id=20, name="STAFF", device="SW-CORE"))
    store.upsert_vlan(VLANRecord(vlan_id=30, name="GUEST", device="SW-CORE"))
    vlans = store.get_vlans(device="SW-CORE")
    assert len(vlans) == 2
    ids = {v.vlan_id for v in vlans}
    assert {20, 30} == ids


def test_store_vlan_exists_on_device():
    store = _store()
    store.upsert_vlan(VLANRecord(vlan_id=40, name="Finance", device="SW-CORE"))
    assert store.vlan_exists_on_device("SW-CORE", 40) is True
    assert store.vlan_exists_on_device("SW-CORE", 99) is False
    assert store.vlan_exists_on_device("SW-ACCESS", 40) is False


def test_store_job_write_and_read():
    from models import JobStatus, ProvisioningJob
    store = _store()
    req   = _req()
    steps = build_plan(req)
    job   = ProvisioningJob(
        job_id     = "test-job-001",
        request    = req,
        status     = JobStatus.SUCCESS,
        steps      = steps,
        created_at = datetime.now(timezone.utc),
    )
    store.write_job(job)
    loaded = store.get_job("test-job-001")
    assert loaded is not None
    assert loaded.status == JobStatus.SUCCESS
    assert len(loaded.steps) == len(steps)


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  [ok] {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
