"""
Test doubles for the device transport.

FakeConnection is deliberately more than a recorder: it keeps enough IOS state
(VLANs, access VLANs, trunk allowed lists) to answer the `show` commands that
pre- and post-change verification issue. That lets the engine tests exercise the
full loop — plan, apply, verify, roll back — without a device.
"""

from __future__ import annotations

from typing import Optional

VLAN_HEADER = "VLAN Name                             Status    Ports"
VLAN_RULE = "---- -------------------------------- --------- -------------------------------"


class FakeConnection:
    """A tiny IOS simulator that records everything sent to it."""

    def __init__(
        self,
        fail_on: Optional[str] = None,
        apply_config: bool = True,
    ) -> None:
        self.fail_on = fail_on
        self.apply_config = apply_config

        self.config_blocks: list[list[str]] = []
        self.commands: list[str] = []
        self.saved = 0
        self.disconnected = False

        # Simulated device state
        self.vlans: dict[int, str] = {}
        self.access: dict[str, int] = {}
        self.trunks: dict[str, set[int]] = {}
        self.modes: dict[str, str] = {}
        self._pending_vlan: Optional[int] = None

    # ── Config path ───────────────────────────────────────────────────────────

    def send_config_set(self, lines, **kwargs):
        block = list(lines)
        self.config_blocks.append(block)
        joined = "\n".join(block)
        if self.fail_on and self.fail_on in joined:
            raise RuntimeError(f"device rejected: {self.fail_on}")
        if self.apply_config:
            self._apply_config(block)
        return f"ok\n{joined}"

    def _apply_config(self, lines: list[str]) -> None:
        current_iface: Optional[str] = None

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("interface "):
                current_iface = line.split(None, 1)[1].strip()
                self._pending_vlan = None
                continue
            if line.startswith("no interface "):
                current_iface = None
                continue

            if current_iface is None:
                if line.startswith("no vlan "):
                    self.vlans.pop(int(line.split()[2]), None)
                    self._pending_vlan = None
                elif line.startswith("vlan ") and line.split()[1].isdigit():
                    vlan_id = int(line.split()[1])
                    self.vlans.setdefault(vlan_id, "")
                    self._pending_vlan = vlan_id
                elif line.startswith("name ") and self._pending_vlan is not None:
                    self.vlans[self._pending_vlan] = line.split(None, 1)[1].strip()
                continue

            if line.startswith("switchport mode access"):
                self.modes[current_iface] = "access"
            elif line.startswith("switchport mode trunk"):
                self.modes[current_iface] = "trunk"
            elif line.startswith("switchport access vlan "):
                self.access[current_iface] = int(line.split()[-1])
                self.modes[current_iface] = "access"
            elif line.startswith("no switchport access vlan"):
                self.access.pop(current_iface, None)
            elif line.startswith("switchport trunk allowed vlan add "):
                self.trunks.setdefault(current_iface, set()).update(
                    _parse_vlan_range(line.split()[-1])
                )
            elif line.startswith("switchport trunk allowed vlan remove "):
                self.trunks.setdefault(current_iface, set()).difference_update(
                    _parse_vlan_range(line.split()[-1])
                )

    # ── Operational path ──────────────────────────────────────────────────────

    def send_command(self, command, expect_string=None, **kwargs):
        self.commands.append(command)
        stripped = command.strip()

        if self.fail_on and self.fail_on in stripped:
            raise RuntimeError(f"device rejected: {self.fail_on}")

        if stripped in ("write memory", "copy running-config startup-config"):
            self.saved += 1
            return "Building configuration...\n[OK]"

        # Single-line rollbacks are sent through send_command.
        if stripped.startswith("no vlan "):
            self.vlans.pop(int(stripped.split()[2]), None)
            return "ok"

        if stripped == "show vlan brief":
            return self._show_vlan_brief()
        if stripped.startswith("show vlan id "):
            return self._show_vlan(int(stripped.split()[-1]))
        if stripped.startswith("show interfaces trunk"):
            return self._show_trunk()
        if stripped.startswith("show interfaces ") and stripped.endswith(" switchport"):
            iface = stripped[len("show interfaces "):-len(" switchport")]
            return self._show_switchport(iface)
        if stripped.startswith("show running-config interface "):
            iface = stripped[len("show running-config interface "):]
            return self._show_running_interface(iface)

        return "ok"

    def _show_vlan_brief(self) -> str:
        lines = [VLAN_HEADER, VLAN_RULE]
        for vlan_id in sorted(self.vlans):
            lines.append(_vlan_row(vlan_id, self.vlans[vlan_id] or f"VLAN{vlan_id:04d}"))
        return "\n".join(lines)

    def _show_vlan(self, vlan_id: int) -> str:
        if vlan_id not in self.vlans:
            return f"% VLAN {vlan_id} not found in current VLAN database"
        return "\n".join([
            VLAN_HEADER,
            VLAN_RULE,
            _vlan_row(vlan_id, self.vlans[vlan_id] or f"VLAN{vlan_id:04d}"),
        ])

    def _show_switchport(self, iface: str) -> str:
        if iface in self.access:
            admin = "static access"
            access_line = f"Access Mode VLAN: {self.access[iface]} (Finance)"
        elif self.modes.get(iface) == "trunk":
            admin = "trunk"
            access_line = "Access Mode VLAN: 1 (default)"
        else:
            admin = "dynamic auto"
            access_line = "Access Mode VLAN: 1 (default)"

        return "\n".join([
            f"Name: {iface}",
            "Switchport: Enabled",
            f"Administrative Mode: {admin}",
            access_line,
        ])

    def _show_trunk(self) -> str:
        if not self.trunks:
            return ""

        lines = ["Port        Mode             Encapsulation  Status        Native vlan"]
        for iface in sorted(self.trunks):
            lines.append(f"{iface:<12}{'on':<17}{'802.1q':<15}{'trunking':<14}1")

        lines.extend(["", "Port        Vlans allowed on trunk"])
        for iface in sorted(self.trunks):
            lines.append(f"{iface:<12}{_format_vlan_range(self.trunks[iface])}")

        lines.extend(["", "Port        Vlans allowed and active in management domain"])
        for iface in sorted(self.trunks):
            lines.append(f"{iface:<12}{_format_vlan_range(self.trunks[iface])}")

        return "\n".join(lines)

    def _show_running_interface(self, iface: str) -> str:
        lines = ["Building configuration...", "", f"interface {iface}"]
        if iface in self.access:
            lines.append(f" switchport access vlan {self.access[iface]}")
        if iface in self.trunks:
            lines.append(
                f" switchport trunk allowed vlan {_format_vlan_range(self.trunks[iface])}"
            )
        if iface in self.modes:
            lines.append(f" switchport mode {self.modes[iface]}")
        lines.append("end")
        return "\n".join(lines)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def enable(self):
        self.commands.append("enable")

    def disconnect(self):
        self.disconnected = True


class FakeTransport:
    """Stands in for `transport.connect`, keyed by hostname."""

    def __init__(self) -> None:
        self.connections: dict[str, FakeConnection] = {}
        self.created: list[tuple[str, FakeConnection]] = []
        self.fail_on: dict[str, Optional[str]] = {}
        self.deaf: set[str] = set()
        self.connect_error: Optional[Exception] = None

    def connect(self, creds):
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(
            fail_on  = self.fail_on.get(creds.hostname),
            apply_config = creds.hostname not in self.deaf,
        )
        self.connections[creds.hostname] = conn
        self.created.append((creds.hostname, conn))
        return conn

    def blocks_for(self, host: str) -> list[str]:
        return [
            "\n".join(block)
            for name, conn in self.created
            if name == host
            for block in conn.config_blocks
        ]

    def commands_for(self, host: str) -> list[str]:
        return [
            command
            for name, conn in self.created
            if name == host
            for command in conn.commands
        ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _vlan_row(vlan_id: int, name: str) -> str:
    return f"{vlan_id:<5}{name:<33}{'active':<10}"


def _parse_vlan_range(vlan_str: str) -> list[int]:
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
    return result


def _format_vlan_range(vlans) -> str:
    values = sorted(set(vlans))
    if not values:
        return "none"

    parts: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        parts.append(_range_part(start, previous))
        start = previous = value
    parts.append(_range_part(start, previous))
    return ",".join(parts)


def _range_part(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"
