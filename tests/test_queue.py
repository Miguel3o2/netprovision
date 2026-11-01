"""Durable queue tests — claims, leases, recovery, and worker dispatch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
from tests.fakes import FakeTransport
from worker import Worker

BODY = {
    "vlan_id":       40,
    "name":          "Finance",
    "switches":      ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1"],
    "access_ports":  ["SW-ACCESS:Fa0/5"],
}


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


def _job(job_id: str = "job-1", status: JobStatus = JobStatus.PENDING) -> ProvisioningJob:
    request = VLANProvisionRequest(
        vlan_id=40,
        name="Finance",
        switches=["SW-CORE", "SW-ACCESS"],
        add_to_trunks=["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
        access_ports=["SW-ACCESS:Fa0/5"],
    )
    return ProvisioningJob(
        job_id     = job_id,
        request    = request,
        status     = status,
        steps      = build_plan(request, None),
        created_at = datetime.now(timezone.utc),
    )


def test_claim_has_exactly_one_winner():
    store = _store()
    store.write_job(_job())

    first = store.claim_next_job("worker-a", lease_seconds=60)
    second = store.claim_next_job("worker-b", lease_seconds=60)

    assert first is not None
    assert first.status is JobStatus.RUNNING
    assert first.lease_owner == "worker-a"
    assert first.attempts == 1
    assert second is None


def test_claim_skips_jobs_that_are_not_approved():
    store = _store()
    store.write_job(_job("job-awaiting", JobStatus.PENDING_APPROVAL))

    assert store.claim_next_job("worker-a") is None


def test_claim_picks_the_oldest_pending_job():
    store = _store()
    older = _job("job-old")
    newer = _job("job-new")
    older.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.write_job(older)
    store.write_job(newer)

    claimed = store.claim_next_job("worker-a")

    assert claimed is not None
    assert claimed.job_id == "job-old"


def test_heartbeat_extends_lease_and_clear_releases_it():
    store = _store()
    store.write_job(_job())
    base = datetime.now(timezone.utc)
    claimed = store.claim_next_job("worker-a", lease_seconds=300, now=base)

    assert claimed is not None
    assert store.heartbeat(claimed.job_id, "someone-else", 300) is False

    assert store.heartbeat(claimed.job_id, "worker-a", 600, now=base) is True
    refreshed = store.get_job(claimed.job_id)
    assert refreshed is not None
    assert refreshed.lease_expires_at > claimed.lease_expires_at

    store.clear_lease(claimed.job_id, "worker-a")
    released = store.get_job(claimed.job_id)
    assert released is not None
    assert released.lease_owner == ""
    assert released.lease_expires_at is None
    assert released.status is JobStatus.RUNNING


def test_expired_lease_is_closed_for_review_not_replayed():
    store = _store()
    store.write_job(_job())
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    store.claim_next_job("vanished-worker", lease_seconds=1, now=base)

    worker = Worker(store, {}, worker_id="replacement", lease_seconds=60)
    assert worker.reclaim() == 1

    job = store.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert "lease expired" in job.error
    assert "vanished-worker" in job.error
    assert job.lease_owner == ""
    assert store.request_rollback(job.job_id, "alice") is True


def test_worker_executes_a_claimed_job_and_clears_the_lease(fake):
    store = _store()
    store.write_job(_job())

    worker = Worker(store, _device_map(), worker_id="worker-a", lease_seconds=60)
    assert worker.run_once() is True

    job = store.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.SUCCESS
    assert job.attempts == 1
    assert job.lease_owner == ""
    assert job.verification_passed is True


def test_worker_reports_an_empty_queue():
    worker = Worker(_store(), {}, worker_id="worker-a")

    assert worker.run_once() is False


def test_worker_executes_a_requested_rollback(fake):
    store = _store()
    job = _job()
    execute(job, store, _device_map())
    assert job.status is JobStatus.SUCCESS

    assert store.request_rollback(job.job_id, "alice") is True
    assert store.request_rollback(job.job_id, "bob") is False

    worker = Worker(store, _device_map(), worker_id="worker-a", lease_seconds=60)
    assert worker.run_once() is True

    rolled = store.get_job(job.job_id)
    assert rolled is not None
    assert rolled.status is JobStatus.ROLLEDBACK
    assert rolled.rollback_requested_by == "alice"
    assert rolled.lease_owner == ""


@pytest.fixture
def worker_api(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi.testclient import TestClient

    import api.server as server

    calls: list[str] = []
    monkeypatch.setattr(
        server, "execute",
        lambda job, store, dm, **kw: calls.append(job.job_id),
    )

    store = _store()
    app = server.build_app(
        store, _device_map(), require_approval=True,
        separation_of_duties=False, queue_mode="worker",
    )
    return TestClient(app), store, calls


def test_worker_mode_enqueues_instead_of_executing(worker_api):
    client, store, calls = worker_api

    response = client.post("/api/vlans/provision", json=BODY, headers={})

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert response.json()["status"] == "pending_approval"
    assert calls == []

    approved = client.post(f"/api/vlans/provision/{job_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["queue"] == "worker"
    assert calls == []

    job = store.get_job(job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.lease_owner == ""


def test_worker_mode_api_rejects_invalid_queue_mode():
    pytest.importorskip("fastapi")
    import api.server as server

    with pytest.raises(ValueError):
        server.build_app(_store(), {}, queue_mode="background-tasks")


def test_worker_mode_rollback_endpoint_only_requests():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi.testclient import TestClient

    import api.server as server

    store = _store()
    job = _job(status=JobStatus.SUCCESS)
    job.steps[0].success = True
    job.steps[0].rollback_cmd = "undo"
    store.write_job(job)

    client = TestClient(server.build_app(store, _device_map(), queue_mode="worker"))
    response = client.post(f"/api/vlans/provision/{job.job_id}/rollback")

    assert response.status_code == 200, response.text
    assert response.json()["queue"] == "worker"
    persisted = store.get_job(job.job_id)
    assert persisted is not None
    assert persisted.rollback_requested_by == "anonymous"
    assert persisted.status is JobStatus.SUCCESS

    duplicate = client.post(f"/api/vlans/provision/{job.job_id}/rollback")
    assert duplicate.status_code == 409
