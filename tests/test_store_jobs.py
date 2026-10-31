"""Store tests — migrations, idempotency, pagination."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from inventory.store import InventoryStore
from models import (
    JobStatus,
    OperationStep,
    OperationType,
    ProvisioningJob,
    VLANProvisionRequest,
)


def _store() -> InventoryStore:
    return InventoryStore(path=":memory:")


def _request(vlan_id: int = 40) -> VLANProvisionRequest:
    return VLANProvisionRequest(
        vlan_id=vlan_id, name="Finance", switches=["SW-CORE"],
    )


def _job(job_id: str, created: datetime, vlan_id: int = 40) -> ProvisioningJob:
    return ProvisioningJob(
        job_id     = job_id,
        request    = _request(vlan_id),
        status     = JobStatus.PENDING,
        created_at = created,
        steps      = [OperationStep(
            device="SW-CORE", operation=OperationType.VLAN_CREATE,
            interface=None, vlan_id=vlan_id, vlan_name="Finance",
            command="vlan 40",
        )],
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_write_and_read_job_roundtrip():
    store = _store()
    job = _job("job-1", _now())
    job.steps[0].success = True
    job.steps[0].output = "ok"
    job.steps[0].rollback_applied = True

    store.write_job(job)
    loaded = store.get_job("job-1")

    assert loaded is not None
    assert loaded.steps[0].output == "ok"
    assert loaded.steps[0].rollback_applied is True
    assert loaded.steps_total == 1


def test_idempotency_key_lookup_and_preservation():
    store = _store()
    job = _job("job-1", _now())

    store.write_job(job, idempotency_key="req-123")
    assert store.get_job_by_idempotency_key("req-123").job_id == "job-1"

    # Later updates must not clear the key.
    store.write_job(job)
    assert store.get_job_by_idempotency_key("req-123").job_id == "job-1"


def test_idempotency_key_is_unique():
    store = _store()
    store.write_job(_job("job-1", _now()), idempotency_key="dup")

    with pytest.raises(sqlite3.IntegrityError):
        store.write_job(_job("job-2", _now()), idempotency_key="dup")


def test_list_jobs_paginates_without_steps():
    store = _store()
    start = _now() - timedelta(minutes=3)
    for i in range(3):
        store.write_job(_job(f"job-{i}", start + timedelta(minutes=i), vlan_id=40 + i))

    page = store.list_jobs(limit=2, offset=0)

    assert [job.job_id for job in page] == ["job-2", "job-1"]
    assert all(job.steps == [] for job in page)
    assert store.count_jobs() == 3


def test_list_jobs_filters_by_status():
    store = _store()
    first = _job("job-1", _now())
    first.status = JobStatus.SUCCESS
    store.write_job(first)
    store.write_job(_job("job-2", _now()))

    assert [job.job_id for job in store.list_jobs(status="success")] == ["job-1"]
    assert store.count_jobs(status="success") == 1


def test_store_migrates_a_pre_existing_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE provisioning_jobs (
            job_id        TEXT PRIMARY KEY,
            vlan_id       INTEGER NOT NULL,
            vlan_name     TEXT NOT NULL,
            switches      TEXT NOT NULL,
            add_to_trunks TEXT NOT NULL,
            access_ports  TEXT NOT NULL,
            requested_by  TEXT DEFAULT 'api',
            status        TEXT NOT NULL,
            steps         TEXT DEFAULT '[]',
            created_at    TEXT,
            completed_at  TEXT,
            error         TEXT
        );
    """)
    conn.commit()
    conn.close()

    store = InventoryStore(str(path))
    try:
        store.write_job(_job("job-1", _now()), idempotency_key="legacy-key")
        assert store.get_job_by_idempotency_key("legacy-key").job_id == "job-1"
        assert store.get_job("job-1").steps_total == 1
    finally:
        store.close()


def test_stats_counts_rows():
    store = _store()
    store.write_job(_job("job-1", _now()))

    stats = store.stats()

    assert stats["jobs"] == 1
    assert stats["devices"] == 0
