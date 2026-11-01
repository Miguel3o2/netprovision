"""Execution tests — run the whole engine against a fake device transport."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import transport
from inventory.store import InventoryStore
from models import (
    DeviceCredentials,
    DeviceType,
    InterfaceMode,
    InterfaceRecord,
    JobStatus,
    ProvisioningJob,
    VLANProvisionRequest,
)
from provisioner.engine import (
    DEVICE_LOCKS,
    VLAN_CLAIMS,
    _send,
    build_plan,
    execute,
    inspect_request,
    rollback,
)
from tests.fakes import FakeConnection, FakeTransport


@pytest.fixture
def fake(monkeypatch):
    fake_transport = FakeTransport()
    monkeypatch.setattr(transport, "connect", fake_transport.connect)
    return fake_transport


def _store() -> InventoryStore:
    return InventoryStore(path=":memory:")


def _device_map() -> dict[str, DeviceCredentials]:
    return {
        "SW-CORE": DeviceCredentials(
            hostname="SW-CORE", ip="192.0.2.2", device_type=DeviceType.CISCO_IOS,
            username="admin", password="lab",
        ),
        "SW-ACCESS": DeviceCredentials(
            hostname="SW-ACCESS", ip="192.0.2.3", device_type=DeviceType.CISCO_IOS,
            username="admin", password="lab",
        ),
    }


def _request(**overrides) -> VLANProvisionRequest:
    values = dict(
        vlan_id=40, name="Finance",
        switches=["SW-CORE", "SW-ACCESS"],
        add_to_trunks=["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
        access_ports=["SW-ACCESS:Fa0/5"],
    )
    values.update(overrides)
    return VLANProvisionRequest(**values)


def _job(request=None, prior=None, job_id="job-1") -> ProvisioningJob:
    request = request or _request()
    return ProvisioningJob(
        job_id     = job_id,
        request    = request,
        status     = JobStatus.PENDING,
        steps      = build_plan(request, prior),
        created_at = datetime.now(timezone.utc),
    )


def test_execute_success_runs_every_step(fake):
    store = _store()
    job = _job()

    execute(job, store, _device_map())

    assert job.status is JobStatus.SUCCESS
    assert all(step.success for step in job.steps)
    assert all(step.output for step in job.steps)
    assert fake.connections["SW-CORE"].saved == 1
    assert fake.connections["SW-ACCESS"].saved == 1

    persisted = store.get_job("job-1")
    assert persisted is not None
    assert persisted.status is JobStatus.SUCCESS
    assert persisted.steps_total == len(job.steps)
    assert all(step.success for step in persisted.steps)


def test_execute_updates_inventory_on_success(fake):
    store = _store()
    execute(_job(), store, _device_map())

    assert store.vlan_exists_on_device("SW-CORE", 40)
    assert store.vlan_exists_on_device("SW-ACCESS", 40)
    assert store.get_interface("SW-ACCESS", "Fa0/5") is not None


def test_execute_runs_in_plan_order(fake):
    store = _store()
    execute(_job(), store, _device_map())

    sent = fake.blocks_for("SW-ACCESS")
    create_index = next(i for i, b in enumerate(sent) if b.startswith("vlan 40"))
    trunk_index = next(i for i, b in enumerate(sent) if "trunk allowed vlan add" in b)
    access_index = next(i for i, b in enumerate(sent) if "switchport mode access" in b)
    assert create_index < trunk_index < access_index


def test_execute_rolls_back_automatically_on_failure(fake):
    fake.fail_on["SW-ACCESS"] = "switchport access vlan 40"
    store = _store()
    job = _job()

    execute(job, store, _device_map())

    assert job.status is JobStatus.ROLLEDBACK
    access = next(s for s in job.steps if s.operation.value == "access_set_vlan")
    assert access.success is False
    assert access.rollback_applied is False

    applied = [s for s in job.steps if s.rollback_applied]
    assert len(applied) == 4
    rollback_output = (
        fake.blocks_for("SW-ACCESS") + fake.blocks_for("SW-CORE")
        + fake.commands_for("SW-ACCESS") + fake.commands_for("SW-CORE")
    )
    assert any("no vlan 40" in entry for entry in rollback_output)
    assert any("trunk allowed vlan remove 40" in entry for entry in rollback_output)

    persisted = store.get_job("job-1")
    assert persisted is not None
    assert persisted.status is JobStatus.ROLLEDBACK


def test_execute_without_auto_rollback_leaves_failed_job(fake):
    fake.fail_on["SW-ACCESS"] = "switchport access vlan 40"
    store = _store()
    job = _job()

    execute(job, store, _device_map(), auto_rollback=False)

    assert job.status is JobStatus.FAILED
    assert not any(step.rollback_applied for step in job.steps)


def test_rollback_restores_previous_access_vlan(fake):
    prior = {
        ("SW-ACCESS", "Fa0/5"): InterfaceRecord(
            device="SW-ACCESS", name="Fa0/5", mode=InterfaceMode.ACCESS,
            access_vlan=20,
        ),
    }
    store = _store()
    job = _job(prior=prior)

    execute(job, store, _device_map())
    assert job.status is JobStatus.SUCCESS

    rollback(job, store, _device_map())

    assert job.status is JobStatus.ROLLEDBACK
    assert any("switchport access vlan 20" in block for block in fake.blocks_for("SW-ACCESS"))
    assert not any("switchport access vlan 1" in block for block in fake.blocks_for("SW-ACCESS"))


def test_rollback_flags_unknown_prior_state_as_manual(fake):
    store = _store()
    job = _job()

    execute(job, store, _device_map())
    rollback(job, store, _device_map())

    access = next(s for s in job.steps if s.operation.value == "access_set_vlan")
    assert access.rollback_applied is False
    assert "manual" in access.rollback_note.lower()


def test_execute_marks_connection_failure(fake):
    fake.connect_error = RuntimeError("connection refused")
    store = _store()
    job = _job()

    execute(job, store, _device_map())

    assert job.status is JobStatus.FAILED
    assert "connection refused" in (job.error or "")
    assert job.steps_attempted == 1


def test_execute_rejects_conflicting_job(fake):
    store = _store()
    blocking = _job(job_id="job-other")
    assert VLAN_CLAIMS.claim(blocking) == []

    try:
        job = _job(job_id="job-conflict")
        execute(job, store, _device_map())
        assert job.status is JobStatus.FAILED
        assert "conflicting" in (job.error or "")
    finally:
        VLAN_CLAIMS.release(blocking)


def test_execute_fails_without_netmiko(fake, monkeypatch):
    monkeypatch.setattr(transport, "NETMIKO_AVAILABLE", False)
    store = _store()
    job = _job()

    execute(job, store, _device_map())

    assert job.status is JobStatus.FAILED
    assert "netmiko" in (job.error or "")


def test_device_locks_are_per_host():
    with DEVICE_LOCKS.hold("SW-CORE"):
        assert DEVICE_LOCKS._lock_for("SW-CORE").locked()
        assert not DEVICE_LOCKS._lock_for("SW-ACCESS").locked()


def test_send_preserves_internal_context_exits():
    conn = FakeConnection()
    _send(conn, "configure terminal\ninterface Gi0/1\nexit\nvlan 40\nexit")

    assert conn.config_blocks[-1] == ["interface Gi0/1", "exit", "vlan 40"]


def test_send_single_command_uses_send_command():
    conn = FakeConnection()
    _send(conn, "write memory")
    assert conn.commands == ["write memory"]


def test_inspect_request_warns_when_inventory_is_empty():
    store = _store()
    steps, warnings = inspect_request(_request(), store)

    assert len(steps) == 5
    assert any("not in the inventory" in warning for warning in warnings)
