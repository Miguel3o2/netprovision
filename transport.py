"""
Device Transport
================
The only module that opens SSH sessions.

The collector and the provisioning engine both go through `connect()` so tests
can substitute a fake transport instead of patching Netmiko internals.
"""

from __future__ import annotations

import logging
from typing import Any

from models import DeviceCredentials

log = logging.getLogger(__name__)

try:
    from netmiko import ConnectHandler
    NETMIKO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when netmiko is absent
    NETMIKO_AVAILABLE = False


def connect(creds: DeviceCredentials) -> Any:
    """Open an SSH session, entering enable mode when a secret is configured."""
    if not NETMIKO_AVAILABLE:
        raise RuntimeError("netmiko is not installed — pip install -r requirements.txt")

    conn = ConnectHandler(
        device_type = creds.device_type.value,
        host        = creds.ip,
        username    = creds.username,
        password    = creds.password,
        secret      = creds.enable_secret or "",
        port        = creds.port,
        timeout     = creds.timeout,
        fast_cli    = False,
    )
    if creds.enable_secret:
        conn.enable()
    return conn
