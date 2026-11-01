"""Verification and snapshot tests — "verify, don't trust" the device."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import transport
from inventory.store import InventoryStore
from models import (
    DeviceCredentials,
    DeviceType,
    JobStatus,
    ProvisioningJob,
    VLANProvisionRequest,
)
from provisioner.engine import build_plan, execute
from tests.fakes import FakeConnection, FakeTransport
from verify import capture_snapshot, verify_request


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


def _request() -> VLANProvisionRequest:
    return VLANProvisionRequest(
        vlan_id=40,
        name="Finance",
        switches=["SW-CORE", "SW-ACCESS"],
        add_to_trunks=["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
        access_ports=["SW-ACCESS:Fa0/5"],
    )


def _job(job_id: str = "job-1") -> ProvisioningJob:
    request = _request()
    return ProvisioningJob(
        job_id     = job_id,
        request    = request,
        status     = JobStatus.PENDING,
        steps      = build_plan(request, None),
        created_at = datetime.now(timezone.utc),
    )


def test_successful_job_records_passing_verification(fake):
    store = _store()
    job = _job()

    execute(job, store, _device_map())

    assert job.status is JobStatus.SUCCESS
    assert job.verification, "verification must produce at least one check"
    assert all(check["ok"] for check in job.verification)
    assert job.verification_passed is True

    checks = {check["check"] for check in job.verification}
    assert "vlan_present" in checks
    assert "access_vlan:Fa0/5" in checks
    assert "trunk_allows:Gi0/1" in checks


def test_verification_is_persisted_with_the_job(fake):
    store = _store()
    job = _job()
    execute(job, store, _device_map())

    persisted = store.get_job("job-1")
    assert persisted is not None
    assert persisted.verification == job.verification
    assert persisted.verification_passed is True


def test_snapshot_captures_touched_interfaces_before_apply(fake):
    store = _store()
    job = _job()
    execute(job, store, _device_map())

    assert set(job.snapshot) == {"SW-CORE", "SW-ACCESS"}

    access = job.snapshot["SW-ACCESS"]
    assert "VLAN Name" in access["vlan"]
    assert "interface Fa0/5" in access["interfaces"]["Fa0/5"]
    assert "switchport access vlan 40" not in access["interfaces"]["Fa0/5"]

    persisted = store.get_job("job-1")
    assert persisted is not None
    assert persisted.snapshot == job.snapshot


def test_job_detail_exposes_snapshot_and_verification():
    job = _job()
    job.verification = [{"device": "SW-CORE", "check": "vlan_present", "ok": True}]
    assert job.verification_passed is True

    job.verification.append({"device": "SW-CORE", "check": "access_vlan", "ok": False})
    assert job.verification_passed is False


def test_device_that_ignores_config_is_caught_and_rolled_back(fake):
    fake.deaf.add("SW-ACCESS")
    store = _store()
    job = _job()

    execute(job, store, _device_map())

    assert job.status is JobStatus.ROLLEDBACK
    assert job.error is not None
    assert job.error.startswith("post-change verification failed")

    failed = [check for check in job.verification if not check["ok"]]
    assert failed
    assert {check["device"] for check in failed} == {"SW-ACCESS"}
    assert "access_vlan:Fa0/5" in {check["check"] for check in failed}

    persisted = store.get_job("job-1")
    assert persisted is not None
    assert persisted.status is JobStatus.ROLLEDBACK
    assert persisted.verification == job.verification


def test_verification_can_be_disabled_for_diagnostic_runs(fake):
    fake.deaf.add("SW-ACCESS")
    store = _store()
    job = _job()

    execute(job, store, _device_map(), verify_after_apply=False)

    assert job.status is JobStatus.SUCCESS
    assert job.verification == []


def test_verification_failure_does_not_save_startup_config_on_failure(fake):
    fake.deaf.add("SW-ACCESS")
    execute(_job(), _store(), _device_map())

    first: dict[str, FakeConnection] = {}
    for host, conn in fake.created:
        first.setdefault(host, conn)
    assert first["SW-ACCESS"].saved == 0


def test_verify_request_reports_missing_vlan_and_trunk():
    state = {
        "show vlan id 40": "% VLAN 40 not found in current VLAN database",
        "show interfaces Fa0/5 switchport": "Access Mode VLAN: 30 (Legacy)",
        "show interfaces trunk": (
            "Port        Vlans allowed on trunk\n"
            "Gi0/1       1,10-12,30\n"
        ),
    }

    checks = verify_request(_request(), lambda host, command: state[command])

    by_check = {check.check: check for check in checks if check.device == "SW-ACCESS"}
    assert by_check["vlan_present"].ok is False
    assert by_check["vlan_present"].actual == "missing"
    assert by_check["access_vlan:Fa0/5"].actual == "30"
    assert by_check["trunk_allows:Gi0/1"].ok is False
    assert by_check["trunk_allows:Gi0/1"].actual == "1,10,11,12,30"


def test_snapshot_truncates_oversized_output():
    huge = "x" * 10_000
    snapshot = capture_snapshot(_request(), lambda host, command: huge, max_chars=100)

    text = snapshot["SW-CORE"]["vlan"]
    assert len(text) < 10_000
    assert "truncated" in text


def test_snapshot_records_command_errors_per_device():
    def send(host: str, command: str) -> str:
        if host == "SW-CORE":
            raise RuntimeError("permission denied")
        return "ok"

    snapshot = capture_snapshot(_request(), send)

    assert "permission denied" in snapshot["SW-CORE"]["error"]
    assert snapshot["SW-ACCESS"]["vlan"] == "ok"
