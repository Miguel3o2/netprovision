"""
Authentication and Authorisation
================================
The API can reconfigure network devices, so it must not be open on a corporate
network. Two modes:

  NP_AUTH_MODE=off      no authentication (labs only — main.py warns)
  NP_AUTH_MODE=apikey   Authorization: Bearer <token>, roles from NP_API_KEYS

Keys are configured out-of-band as environment variables:

  NP_API_KEYS="alice:<token>:operator,bob:<token>:approver,carol:<token>:admin"

Format is `name:token:role`, comma-separated. Tokens are compared in constant
time and should be high-entropy random strings (e.g. `python -c "import
secrets; print(secrets.token_urlsafe(32))"`).

Roles are permission sets, not a hierarchy:

  viewer    read
  operator  read, write
  approver  read, approve
  admin     read, write, approve

OIDC/JWT validation is the next milestone; `authenticate()` is the single seam
it will plug into.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional


class Unauthorized(Exception):
    """No credentials, or credentials that do not match a configured key."""


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer":   frozenset({"read"}),
    "operator": frozenset({"read", "write"}),
    "approver": frozenset({"read", "approve"}),
    "admin":    frozenset({"read", "write", "approve"}),
}


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    name: str
    role: str

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def can(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass
class AuthConfig:
    """Resolved authentication settings."""

    mode: str = "off"
    keys: Mapping[str, Principal] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "AuthConfig":
        env = os.environ if environ is None else environ
        mode = (env.get("NP_AUTH_MODE") or "off").strip().lower()
        if mode not in ("off", "apikey"):
            raise ValueError(
                f"NP_AUTH_MODE='{mode}' is not supported (expected 'off' or 'apikey')"
            )

        keys: dict[str, Principal] = {}
        raw_keys = (env.get("NP_API_KEYS") or "").strip()
        if raw_keys:
            for entry in raw_keys.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split(":", 2)
                if len(parts) != 3 or not all(parts):
                    raise ValueError(
                        "NP_API_KEYS entries must look like 'name:token:role'"
                    )
                name, token, role = (part.strip() for part in parts)
                if role not in ROLE_PERMISSIONS:
                    raise ValueError(
                        f"NP_API_KEYS role '{role}' is unknown "
                        f"(expected one of {', '.join(sorted(ROLE_PERMISSIONS))})"
                    )
                keys[token] = Principal(name=name, role=role)

        if mode == "apikey" and not keys:
            raise ValueError("NP_AUTH_MODE=apikey requires at least one NP_API_KEYS entry")

        return cls(mode=mode, keys=keys)


def authenticate(
    authorization: Optional[str],
    config: Optional[AuthConfig] = None,
) -> Principal:
    """
    Resolve the caller from an `Authorization` header.

    In `off` mode every caller is an admin, preserving lab behaviour.
    Raises Unauthorized for missing/malformed/unknown credentials.
    """
    config = config or AuthConfig()
    if not config.enabled:
        return Principal(name="anonymous", role="admin")

    if not authorization:
        raise Unauthorized("missing Authorization header (expected Bearer token)")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("Authorization header must be 'Bearer <token>'")

    candidate = token.strip()
    for configured, principal in config.keys.items():
        if hmac.compare_digest(configured, candidate):
            return principal

    raise Unauthorized("invalid API key")
