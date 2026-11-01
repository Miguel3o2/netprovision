"""API tests — exercised through FastAPI's TestClient, no devices involved."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import api.server as server  # noqa: E402
from inventory.store import InventoryStore  # noqa: E402
from models import DeviceCredentials, DeviceType  # noqa: E402

BODY = {
    "vlan_id":       40,
    "name":          "Finance",
    "switches":      ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1"],
    "access_ports":  ["SW-ACCESS:Fa0/5"],
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(server, "execute", lambda job, store, dm, **kw: job)

    store = InventoryStore(path=":memory:")
    device_map = {
        "SW-CORE": DeviceCredentials(
            hostname="SW-CORE", ip="192.0.2.2", device_type=DeviceType.CISCO_IOS,
        ),
        "SW-ACCESS": DeviceCredentials(
            hostname="SW-ACCESS", ip="192.0.2.3", device_type=DeviceType.CISCO_IOS,
        ),
    }
    return TestClient(server.build_app(store, device_map)), store


def test_healthz(api):
    client, _ = api
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_store(api):
    client, _ = api
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_metrics_exposes_prometheus_text(api):
    client, _ = api
    client.get("/healthz")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "np_http_requests_total" in response.text
    assert "np_jobs_total" in response.text


def test_request_id_is_echoed(api):
    client, _ = api
    response = client.get("/healthz", headers={"X-Request-ID": "trace-1"})
    assert response.headers["X-Request-ID"] == "trace-1"


def test_plan_returns_commands_without_creating_a_job(api):
    client, store = api
    response = client.post("/api/vlans/plan", json=BODY)

    assert response.status_code == 200
    data = response.json()
    assert data["touches_devices"] is False
    assert data["step_count"] == 4
    assert "vlan 40" in data["steps"][0]["commands"]
    assert data["steps"][0]["rollback_commands"] == ["configure terminal", "no vlan 40", "exit"]
    assert store.count_jobs() == 0


def test_plan_warns_when_inventory_is_empty(api):
    client, _ = api
    response = client.post("/api/vlans/plan", json=BODY)

    assert response.status_code == 200
    assert any("not in the inventory" in warning for warning in response.json()["warnings"])


def test_plan_rejects_reserved_vlan(api):
    client, _ = api
    response = client.post("/api/vlans/plan", json={**BODY, "vlan_id": 1})
    assert response.status_code == 422


def test_plan_rejects_spaces_in_name(api):
    client, _ = api
    response = client.post("/api/vlans/plan", json={**BODY, "name": "my vlan"})
    assert response.status_code == 422
    assert "spaces" in str(response.json())


def test_provision_creates_a_job(api):
    client, store = api
    response = client.post("/api/vlans/provision", json=BODY)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "pending"
    assert data["steps"] == 4
    assert store.get_job(data["job_id"]) is not None


def test_provision_is_idempotent_with_a_key(api):
    client, store = api
    headers = {"Idempotency-Key": "change-4711"}

    first = client.post("/api/vlans/provision", json=BODY, headers=headers)
    second = client.post("/api/vlans/provision", json=BODY, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["job_id"] == first.json()["job_id"]
    assert store.count_jobs() == 1


def test_provision_rejects_malformed_idempotency_key(api):
    client, _ = api
    response = client.post(
        "/api/vlans/provision", json=BODY, headers={"Idempotency-Key": "not a key!"},
    )
    assert response.status_code == 400


def test_provision_validates_before_queueing(api):
    client, store = api
    response = client.post("/api/vlans/provision", json={**BODY, "vlan_id": 1002})

    assert response.status_code == 422
    assert store.count_jobs() == 0


def test_job_status_detail_includes_evidence(api):
    client, _ = api
    job_id = client.post("/api/vlans/provision", json=BODY).json()["job_id"]

    response = client.get(f"/api/vlans/provision/{job_id}")

    assert response.status_code == 200
    steps = response.json()["steps"]
    assert len(steps) == 4
    assert steps[0]["command"].startswith("configure terminal")
    assert "rollback_note" in steps[0]


def test_unknown_job_returns_404(api):
    client, _ = api
    assert client.get("/api/vlans/provision/nope").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_rollback_rejects_a_pending_job(api):
    client, _ = api
    job_id = client.post("/api/vlans/provision", json=BODY).json()["job_id"]

    response = client.post(f"/api/vlans/provision/{job_id}/rollback")

    assert response.status_code == 409


def test_jobs_list_is_paginated_and_omits_steps(api):
    client, _ = api
    client.post("/api/vlans/provision", json=BODY)

    response = client.get("/api/jobs?limit=10&offset=0")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["limit"] == 10
    assert "steps" not in data["jobs"][0]
    assert data["jobs"][0]["steps_total"] == 4


def test_jobs_list_rejects_unknown_status(api):
    client, _ = api
    assert client.get("/api/jobs?status=exploded").status_code == 400
