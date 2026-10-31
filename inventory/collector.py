"""
Inventory Collector
===================
SSHs into devices and parses show commands to build the inventory:
  show version          → platform, model, serial, uptime
  show interfaces       → interface list with status, IP, speed
  show interfaces trunk → trunk mode interfaces with allowed VLANs
  show interfaces status→ access VLAN per port
  show vlan brief       → VLAN database

Parsing is done with regex against real IOS output.
"""

from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from models import (
    DeviceCredentials, DeviceRecord, InterfaceRecord,
    VLANRecord, InterfaceMode, InterfaceStatus, VLANStatus,
)
from inventory.store import InventoryStore

log = logging.getLogger(__name__)

try:
    from netmiko import ConnectHandler
    NETMIKO_AVAILABLE = True
except ImportError:
    NETMIKO_AVAILABLE = False


# ── Command outputs we collect ────────────────────────────────────────────────

COMMANDS = {
    "version":    "show version",
    "int_status": "show interfaces status",
    "trunk":      "show interfaces trunk",
    "vlan":       "show vlan brief",
    "ip_int":     "show ip interface brief",
}


def collect(device: DeviceCredentials, store: InventoryStore) -> Optional[DeviceRecord]:
    """
    SSH into a device, run show commands, parse output, write to store.
    Returns the DeviceRecord on success, None on failure.
    """
    if not NETMIKO_AVAILABLE:
        log.warning("netmiko not installed — cannot collect inventory")
        return None

    try:
        with ConnectHandler(
            device_type = device.device_type.value,
            host        = device.ip,
            username    = device.username,
            password    = device.password,
            secret      = device.enable_secret or "",
            port        = device.port,
            timeout     = device.timeout,
            fast_cli    = False,
        ) as conn:
            if device.enable_secret:
                conn.enable()

            outputs = {key: conn.send_command(cmd) for key, cmd in COMMANDS.items()}

    except Exception as e:
        log.error(f"{device.hostname}: collection failed — {e}")
        return None

    now = datetime.now(timezone.utc)

    # ── Parse version ─────────────────────────────────────────────────────────
    rec = DeviceRecord(
        hostname    = device.hostname,
        ip          = device.ip,
        device_type = device.device_type.value,
        tags        = device.tags,
        last_seen   = now,
    )

    ver = outputs.get("version", "")
    m = re.search(r"Cisco IOS[^\n]*Version\s+([\S]+)", ver, re.I)
    if m:
        rec.platform = f"Cisco IOS {m.group(1)}"
    m = re.search(r"Model number\s*:\s*([\S]+)", ver, re.I)
    if m:
        rec.model = m.group(1)
    m = re.search(r"Processor board ID\s+([\S]+)", ver, re.I)
    if m:
        rec.serial = m.group(1)
    m = re.search(r"uptime is (.+)", ver, re.I)
    if m:
        rec.uptime = m.group(1).strip()

    # ── Parse interfaces ───────────────────────────────────────────────────────
    ifaces: dict[str, InterfaceRecord] = {}

    int_status = outputs.get("int_status", "")
    for line in int_status.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        status = "up" if "connected" in line.lower() else "down"
        vlan_str = parts[2] if len(parts) > 2 else ""
        access_vlan = None
        mode = InterfaceMode.UNKNOWN
        if vlan_str.isdigit():
            access_vlan = int(vlan_str)
            mode = InterfaceMode.ACCESS
        elif vlan_str.lower() in ("trunk", "routed"):
            mode = InterfaceMode(vlan_str.lower()) if vlan_str.lower() in ("trunk", "routed") else InterfaceMode.TRUNK

        ifaces[name] = InterfaceRecord(
            device      = device.hostname,
            name        = name,
            mode        = mode,
            status      = InterfaceStatus(status),
            access_vlan = access_vlan,
            discovered  = now,
        )

    # Parse trunk interfaces for allowed VLANs
    trunk_out = outputs.get("trunk", "")
    # "Port        Mode         Encapsulation  Status        Native vlan"
    # "Gi0/1       on           802.1q         trunking      99"
    # Then later: "Port        Vlans allowed on trunk"
    #             "Gi0/1       1,10,20,30,99"
    allowed_section = False
    for line in trunk_out.splitlines():
        if "Vlans allowed on trunk" in line:
            allowed_section = True
            continue
        if allowed_section and line.strip() and not line.startswith(" ") and not line[0].isalpha():
            allowed_section = False
        if allowed_section:
            parts = line.split()
            if len(parts) >= 2:
                iface_name = parts[0]
                vlan_str   = parts[1]
                vlans = _parse_vlan_range(vlan_str)
                if iface_name in ifaces:
                    ifaces[iface_name].trunk_vlans = vlans
                    ifaces[iface_name].mode = InterfaceMode.TRUNK
                else:
                    ifaces[iface_name] = InterfaceRecord(
                        device      = device.hostname,
                        name        = iface_name,
                        mode        = InterfaceMode.TRUNK,
                        status      = InterfaceStatus.UNKNOWN,
                        trunk_vlans = vlans,
                        discovered  = now,
                    )

    # Parse IP addresses
    ip_brief = outputs.get("ip_int", "")
    for line in ip_brief.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in ("unassigned", "YES"):
            name = parts[0]
            ip   = parts[1]
            if name in ifaces:
                ifaces[name].ip_address = ip
            else:
                ifaces[name] = InterfaceRecord(
                    device     = device.hostname,
                    name       = name,
                    mode       = InterfaceMode.ROUTED,
                    ip_address = ip,
                    discovered = now,
                )

    # ── Parse VLANs ───────────────────────────────────────────────────────────
    vlans: list[VLANRecord] = []
    vlan_out = outputs.get("vlan", "")
    for line in vlan_out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(active|act\/unsup)", line, re.I)
        if m:
            vlans.append(VLANRecord(
                vlan_id    = int(m.group(1)),
                name       = m.group(2),
                status     = VLANStatus.ACTIVE,
                device     = device.hostname,
                discovered = now,
            ))

    # ── Write to store ────────────────────────────────────────────────────────
    store.upsert_device(rec)
    for iface in ifaces.values():
        store.upsert_interface(iface)
    for vlan in vlans:
        store.upsert_vlan(vlan)

    log.info(f"{device.hostname}: collected {len(ifaces)} interfaces, {len(vlans)} VLANs")
    return rec


def _parse_vlan_range(vlan_str: str) -> list[int]:
    """Parse IOS VLAN range string '1,10-20,30,99' → [1,10,11,...,20,30,99]."""
    result = []
    for part in vlan_str.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                result.extend(range(int(start), int(end) + 1))
            except ValueError:
                pass
        elif part.isdigit():
            result.append(int(part))
    return sorted(set(result))
