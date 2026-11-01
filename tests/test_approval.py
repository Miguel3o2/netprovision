"""Approval workflow tests — request, approve, reject, separation of duties."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import api.server as server  # noqa: E402
from auth import AuthConfig, Principal  # noqa: E402
from inventory.store import InventoryStore  # noqa: E402
from models import DeviceCredentials, DeviceType, JobStatus  # noqa: E402

BODY = {
    "vlan_id":       40,
    "name":          "Finance",
    "switches":      ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1"],
    "access_ports":  ["SW-ACCESS:Fa0/5"],
}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def approval(monkeypatch):
    """Auth on, approval required, separation of duties enforced."""
    monkeypatch.setattr(server, "execute", lambda job, store, dm, **kw: job)

    config = AuthConfig(mode="apikey", keys={
        "alice-token": Principal("alice", "operator"),
        "bob-token":   Principal("bob", "approver"),
        "alice-admin": Principal("alice", "admin"),
    })

    store = InventoryStore(path=":memory:")
    device_map = {
        "SW-CORE": DeviceCredentials(
            hostname="SW-CORE", ip="192.0.2.2", device_type=DeviceType.CISCO_IOS,
        ),
        "SW-ACCESS": DeviceCredentials(
            hostname="SW-ACCESS", ip="192.0.2.3", device_type=DeviceType.CISCO_IOS,
        ),
    }
    app = server.build_app(
        store, device_map,
        auth_config=config,
        require_approval=True,
        separation_of_duties=True,
    )
    return TestClient(app), store


def _request(client) -> str:
    response = client.post(
        "/api/vlans/provision", json=BODY, headers=_bearer("alice-token"),
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _approve(client, job_id: str, token: str, comment: str = ""):
    return client.post(
        f"/api/vlans/provision/{job_id}/approve",
        json={"comment": comment},
        headers=_bearer(token),
    )


def _reject(client, job_id: str, token: str, comment: str = ""):
    return client.post(
        f"/api/vlans/provision/{job_id}/reject",
        json={"comment": comment},
        headers=_bearer(token),
    )


def test_provision_waits_for_approval_and_does_not_touch_devices(approval):
    client, store = approval
    response = client.post(
        "/api/vlans/provision", json=BODY, headers=_bearer("alice-token"),
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "pending_approval"
    assert data["approval_required"] is True
    assert data["requested_by"] == "alice"

    job = store.get_job(data["job_id"])
    assert job is not None
    assert job.status is JobStatus.PENDING_APPROVAL
    assert job.steps_succeeded == 0
    assert job.approved_by == ""


def test_pending_job_appears_in_job_listing(approval):
    client, _ = approval
    job_id = _request(client)

    response = client.get(
        "/api/jobs", params={"status": "pending_approval"}, headers=_bearer("bob-token"),
    )

    assert response.status_code == 200
    assert [job["job_id"] for job in response.json()["jobs"]] == [job_id]


def test_approver_releases_the_job(approval):
    client, store = approval
    job_id = _request(client)

    response = _approve(client, job_id, "bob-token", comment="CAB-1234")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert data["approved_by"] == "bob"

    job = store.get_job(job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.approved_by == "bob"
    assert job.approved_at is not None
    assert job.approval_comment == "CAB-1234"


def test_job_detail_exposes_approval_evidence(approval):
    client, _ = approval
    job_id = _request(client)
    _approve(client, job_id, "bob-token", comment="CAB-1234")

    detail = client.get(
        f"/api/vlans/provision/{job_id}", headers=_bearer("alice-token"),
    ).json()

    assert detail["approved_by"] == "bob"
    assert detail["approval_comment"] == "CAB-1234"
    assert detail["approved_at"]


def test_requester_cannot_approve_their_own_change(approval):
    client, store = approval
    job_id = _request(client)

    response = _approve(client, job_id, "alice-admin")

    assert response.status_code == 409, response.text
    assert "separation of duties" in response.json()["detail"]
    assert store.get_job(job_id).status is JobStatus.PENDING_APPROVAL


def test_operator_cannot_approve(approval):
    client, store = approval
    job_id = _request(client)

    response = _approve(client, job_id, "alice-token")

    assert response.status_code == 403
    assert store.get_job(job_id).status is JobStatus.PENDING_APPROVAL


def test_a_job_cannot_be_approved_twice(approval):
    client, _ = approval
    job_id = _request(client)
    assert _approve(client, job_id, "bob-token").status_code == 200

    response = _approve(client, job_id, "bob-token")

    assert response.status_code == 409
    assert "not awaiting approval" in response.json()["detail"]


def test_unknown_job_is_404(approval):
    client, _ = approval
    assert _approve(client, "no-such-job", "bob-token").status_code == 404


def test_rejection_cancels_the_job_with_a_reason(approval):
    client, store = approval
    job_id = _request(client)

    response = _reject(client, job_id, "bob-token", comment="wrong VLAN for this site")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    job = store.get_job(job_id)
    assert job is not None
    assert job.status is JobStatus.CANCELLED
    assert job.completed_at is not None
    assert job.error == "rejected by bob: wrong VLAN for this site"


def test_cancelled_job_cannot_be_approved_or_rolled_back(approval):
    client, _ = approval
    job_id = _request(client)
    assert _reject(client, job_id, "bob-token").status_code == 200

    assert _approve(client, job_id, "bob-token").status_code == 409
    assert client.post(
        f"/api/vlans/provision/{job_id}/rollback", headers=_bearer("alice-admin"),
    ).status_code == 409


def test_cancel_requires_approver_role(approval):
    client, store = approval
    job_id = _request(client)

    assert _reject(client, job_id, "alice-token").status_code == 403
    assert store.get_job(job_id).status is JobStatus.PENDING_APPROVAL
