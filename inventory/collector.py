"""
Inventory Collector
==================
SSHs into devices and parses show commands to build the inventory:
  show version          → platform, model, serial, uptime
  show interfaces status→ interface list, access VLAN, status
  show interfaces trunk → trunk interfaces with allowed VLANs
  show ip interface brief → routed interface addresses
  show vlan brief       → VLAN database

Parsing is column-aware because IOS pads columns with spaces: interface names
and descriptions may themselves contain single spaces or dots.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional, Sequence

import transport
from inventory.store import InventoryStore
from models import (
    DeviceCredentials,
    DeviceRecord,
    InterfaceMode,
    InterfaceRecord,
    InterfaceStatus,
    VLANRecord,
    VLANStatus,
)

log = logging.getLogger(__name__)

COMMANDS = {
    "version":    "show version",
    "int_status": "show interfaces status",
    "trunk":      "show interfaces trunk",
    "vlan":       "show vlan brief",
    "ip_int":     "show ip interface brief",
}

STATUS_COLUMNS = ("Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type")
CANONICAL_HEADER = (
    "Port      Name               Status       Vlan       Duplex  Speed Type"
)
VLAN_COLUMNS = ("VLAN", "Name", "Status", "Ports")
CANONICAL_VLAN_HEADER = "VLAN Name" + " " * 29 + "Status    Ports"

IFACE_RE = re.compile(r"^[A-Za-z][\w\-./]*\d")


def collect(device: DeviceCredentials, store: InventoryStore) -> Optional[DeviceRecord]:
    """
    SSH into a device, run show commands, parse output, write to store.
    Returns the DeviceRecord on success, None on failure.
    """
    if not transport.NETMIKO_AVAILABLE:
        log.warning("netmiko not installed — cannot collect inventory")
        return None

    conn = None
    try:
        conn = transport.connect(device)
        outputs = {key: conn.send_command(cmd) for key, cmd in COMMANDS.items()}
    except Exception as exc:
        log.error(f"{device.hostname}: collection failed — {exc}")
        return None
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:  # pragma: no cover - teardown best effort
                pass

    now = datetime.now(timezone.utc)

    rec = DeviceRecord(
        hostname    = device.hostname,
        ip          = device.ip,
        device_type = device.device_type.value,
        tags        = device.tags,
        last_seen   = now,
    )
    for field, value in parse_version(outputs.get("version", "")).items():
        setattr(rec, field, value)

    ifaces = parse_interfaces_status(device.hostname, outputs.get("int_status", ""), now)
    apply_trunk_output(device.hostname, outputs.get("trunk", ""), ifaces, now)
    apply_ip_brief(device.hostname, outputs.get("ip_int", ""), ifaces, now)
    vlans = parse_vlans(device.hostname, outputs.get("vlan", ""), now)

    store.upsert_device(rec)
    for iface in ifaces.values():
        store.upsert_interface(iface)
    for vlan in vlans:
        store.upsert_vlan(vlan)

    log.info(f"{device.hostname}: collected {len(ifaces)} interfaces, {len(vlans)} VLANs")
    return rec


def parse_version(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if m := re.search(r"Cisco IOS(?: XE)?[^\n]*Version\s+([^,\s]+)", text, re.I):
        result["platform"] = f"Cisco IOS {m.group(1)}"
    if m := re.search(r"Model number\s*:\s*(\S+)", text, re.I):
        result["model"] = m.group(1)
    if m := re.search(r"Processor board ID\s+(\S+)", text, re.I):
        result["serial"] = m.group(1)
    if m := re.search(r"uptime is (.+)", text, re.I):
        result["uptime"] = m.group(1).strip()
    return result


def parse_interfaces_status(
    device: str,
    text: str,
    now:    Optional[datetime] = None,
) -> dict[str, InterfaceRecord]:
    """Parse `show interfaces status` using the header's column offsets."""
    now = now or datetime.now(timezone.utc)
    positions: Optional[dict[str, int]] = None
    data_lines: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if positions is None and _looks_like_header(line):
            positions = _column_positions(line, STATUS_COLUMNS)
            if positions is not None:
                continue
        data_lines.append(line)

    if positions is None:
        positions = _column_positions(CANONICAL_HEADER, STATUS_COLUMNS)

    ifaces: dict[str, InterfaceRecord] = {}
    for line in data_lines:
        fields = _slice_columns(line, positions or {}, STATUS_COLUMNS)
        name = fields["Port"]
        if not IFACE_RE.match(name):
            continue

        vlan_field = fields.get("Vlan", "")
        mode = InterfaceMode.UNKNOWN
        access_vlan: Optional[int] = None
        if vlan_field.isdigit():
            access_vlan = int(vlan_field)
            mode = InterfaceMode.ACCESS
        elif vlan_field.lower() == "trunk":
            mode = InterfaceMode.TRUNK
        elif vlan_field.lower() == "routed":
            mode = InterfaceMode.ROUTED

        ifaces[name] = InterfaceRecord(
            device      = device,
            name        = name,
            description = fields.get("Name", ""),
            mode        = mode,
            status      = _status_from_text(fields.get("Status", "")),
            access_vlan = access_vlan,
            speed       = fields.get("Speed") or None,
            duplex      = fields.get("Duplex") or None,
            discovered  = now,
        )

    return ifaces


def parse_trunk_allowed(text: str) -> dict[str, list[int]]:
    """
    Parse the 'Vlans allowed on trunk' table of `show interfaces trunk`.

    Used by both the inventory collector and post-change verification, so both
    agree on what "the trunk allows VLAN 40" means.
    """
    allowed: dict[str, list[int]] = {}
    in_allowed = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"^Port\s+Vlans allowed on trunk", line, re.I):
            in_allowed = True
            continue
        if line.lower().startswith("port"):
            in_allowed = False
            continue
        if not in_allowed:
            continue

        parts = line.split()
        if len(parts) < 2 or not IFACE_RE.match(parts[0]):
            continue
        allowed[parts[0]] = _parse_vlan_range(parts[1])
    return allowed


def apply_trunk_output(
    device: str,
    text:   str,
    ifaces: dict[str, InterfaceRecord],
    now:    Optional[datetime] = None,
) -> None:
    """Fold `show interfaces trunk` data into an interface map."""
    now = now or datetime.now(timezone.utc)

    for iface_name, vlans in parse_trunk_allowed(text).items():
        if iface_name in ifaces:
            ifaces[iface_name].trunk_vlans = vlans
            ifaces[iface_name].mode = InterfaceMode.TRUNK
        else:
            ifaces[iface_name] = InterfaceRecord(
                device      = device,
                name        = iface_name,
                mode        = InterfaceMode.TRUNK,
                status      = InterfaceStatus.UNKNOWN,
                trunk_vlans = vlans,
                discovered  = now,
            )

    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) >= 5 and IFACE_RE.match(parts[0]) and parts[3].lower() == "trunking":
            if parts[0] in ifaces and parts[4].isdigit():
                ifaces[parts[0]].native_vlan = int(parts[4])


def apply_ip_brief(
    device: str,
    text:   str,
    ifaces: dict[str, InterfaceRecord],
    now:    Optional[datetime] = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    for raw in text.splitlines()[1:]:
        parts = raw.split()
        if len(parts) < 2 or not IFACE_RE.match(parts[0]):
            continue
        name, ip = parts[0], parts[1]
        if ip.lower() == "unassigned":
            continue
        if name in ifaces:
            ifaces[name].ip_address = ip
        else:
            ifaces[name] = InterfaceRecord(
                device     = device,
                name       = name,
                mode       = InterfaceMode.ROUTED,
                ip_address = ip,
                discovered = now,
            )


def parse_vlans(
    device: str,
    text:   str,
    now:    Optional[datetime] = None,
) -> list[VLANRecord]:
    now = now or datetime.now(timezone.utc)

    positions: Optional[dict[str, int]] = None
    data_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if positions is None and line.lstrip().startswith("VLAN") and "Status" in line:
            positions = _column_positions(line, VLAN_COLUMNS)
            if positions is not None:
                continue
        data_lines.append(line)

    if positions is None:
        positions = _column_positions(CANONICAL_VLAN_HEADER, VLAN_COLUMNS)

    vlans: list[VLANRecord] = []
    seen: set[int] = set()

    for line in data_lines:
        fields = _slice_columns(line, positions or {}, VLAN_COLUMNS)
        vlan_field = fields["VLAN"]
        if not vlan_field.isdigit():
            continue
        vlan_id = int(vlan_field)
        if vlan_id in seen:
            continue
        status_text = fields.get("Status", "").lower()
        seen.add(vlan_id)
        vlans.append(VLANRecord(
            vlan_id    = vlan_id,
            name       = fields.get("Name", ""),
            status     = VLANStatus.INACTIVE if status_text.startswith("act/unsup") else VLANStatus.ACTIVE,
            device     = device,
            discovered = now,
        ))
    return vlans


def _column_positions(
    header:  str,
    columns: Sequence[str],
) -> Optional[dict[str, int]]:
    positions: dict[str, int] = {}
    cursor = 0
    for column in columns:
        idx = header.find(column, cursor)
        if idx < 0:
            return None
        positions[column] = idx
        cursor = idx + len(column)
    return positions


def _slice_columns(
    line:      str,
    positions: dict[str, int],
    columns:   Sequence[str],
) -> dict[str, str]:
    fields: dict[str, str] = {}
    for i, column in enumerate(columns):
        start = positions[column]
        end = positions[columns[i + 1]] if i + 1 < len(columns) else None
        fields[column] = (line[start:end] if end is not None else line[start:]).strip()
    return fields


def _looks_like_header(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("Port") and "Status" in line and "Vlan" in line


def _status_from_text(text: str) -> InterfaceStatus:
    value = text.strip().lower()
    if not value:
        return InterfaceStatus.UNKNOWN
    if value.startswith("connected"):
        return InterfaceStatus.UP
    return InterfaceStatus.DOWN


def _parse_vlan_range(vlan_str: str) -> list[int]:
    """Parse IOS VLAN range string '1,10-20,30,99' → [1,10,11,...,20,30,99]."""
    result: list[int] = []
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
