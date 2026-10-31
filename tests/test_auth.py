"""Authentication and role tests."""

from __future__ import annotations

import pytest

from auth import AuthConfig, Principal, Unauthorized, authenticate

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


def _config(**env: str) -> AuthConfig:
    return AuthConfig.from_env(env)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Unit: configuration and principals ────────────────────────────────────────

def test_off_mode_treats_everyone_as_admin():
    principal = authenticate(None, AuthConfig())
    assert principal.name == "anonymous"
    assert principal.role == "admin"
    assert principal.can("write")
    assert principal.can("approve")


def test_api_keys_are_parsed_into_principals():
    config = _config(
        NP_AUTH_MODE="apikey",
        NP_API_KEYS="alice:tok-a:operator, bob:tok-b:approver",
    )

    assert config.enabled
    assert set(config.keys) == {"tok-a", "tok-b"}
    assert authenticate("Bearer tok-a", config).name == "alice"
    assert authenticate("Bearer tok-b", config).role == "approver"


@pytest.mark.parametrize("header", [
    None,
    "",
    "tok-a",
    "Basic tok-a",
    "Bearer ",
    "Bearer wrong-token",
])
def test_missing_or_wrong_credentials_are_rejected(header):
    config = _config(NP_AUTH_MODE="apikey", NP_API_KEYS="alice:tok-a:admin")
    with pytest.raises(Unauthorized):
        authenticate(header, config)


@pytest.mark.parametrize("env", [
    {"NP_AUTH_MODE": "ldap"},
    {"NP_AUTH_MODE": "apikey"},
    {"NP_AUTH_MODE": "apikey", "NP_API_KEYS": "broken"},
    {"NP_AUTH_MODE": "apikey", "NP_API_KEYS": "alice:tok:wizard"},
])
def test_invalid_auth_configuration_fails_fast(env):
    with pytest.raises(ValueError):
        AuthConfig.from_env(env)


def test_roles_are_permission_sets_not_a_hierarchy():
    viewer = Principal("vera", "viewer")
    operator = Principal("otto", "operator")
    approver = Principal("anna", "approver")

    assert viewer.can("read")
    assert not viewer.can("write")
    assert operator.can("write")
    assert not operator.can("approve")
    assert approver.can("approve")
    assert not approver.can("write")


def test_unknown_role_has_no_permissions():
    assert Principal("nobody", "intern").permissions == frozenset()


# ── API: authentication and authorisation ─────────────────────────────────────

@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setattr(server, "execute", lambda job, store, dm, **kw: job)

    config = AuthConfig(mode="apikey", keys={
        "viewer-token":   Principal("vera", "viewer"),
        "operator-token": Principal("otto", "operator"),
        "approver-token": Principal("anna", "approver"),
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
    client = TestClient(server.build_app(store, device_map, auth_config=config))
    return client, store


def test_health_and_metrics_stay_public(secured):
    client, _ = secured
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_missing_token_is_401_with_bearer_challenge(secured):
    client, _ = secured
    response = client.get("/api/inventory/devices")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_401(secured):
    client, _ = secured
    response = client.get("/api/inventory/devices", headers=_bearer("nope"))
    assert response.status_code == 401


def test_viewer_can_read_inventory(secured):
    client, _ = secured
    response = client.get("/api/inventory/devices", headers=_bearer("viewer-token"))
    assert response.status_code == 200


def test_viewer_cannot_provision(secured):
    client, store = secured
    response = client.post("/api/vlans/provision", json=BODY, headers=_bearer("viewer-token"))

    assert response.status_code == 403
    assert "write" in response.json()["detail"]
    assert store.count_jobs() == 0


def test_operator_can_plan_without_side_effects(secured):
    client, store = secured
    response = client.post("/api/vlans/plan", json=BODY, headers=_bearer("operator-token"))

    assert response.status_code == 200
    assert store.count_jobs() == 0


def test_operator_can_provision(secured):
    client, _ = secured
    response = client.post(
        "/api/vlans/provision", json=BODY, headers=_bearer("operator-token"),
    )
    assert response.status_code == 202


def test_approver_cannot_provision(secured):
    client, _ = secured
    response = client.post(
        "/api/vlans/provision", json=BODY, headers=_bearer("approver-token"),
    )
    assert response.status_code == 403


def test_requested_by_comes_from_the_token_not_the_body(secured):
    client, store = secured
    body = {**BODY, "requested_by": "someone-else"}

    response = client.post("/api/vlans/provision", json=body, headers=_bearer("operator-token"))

    assert response.status_code == 202
    job = store.get_job(response.json()["job_id"])
    assert job is not None
    assert job.request.requested_by == "otto"
