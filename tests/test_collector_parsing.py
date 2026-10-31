"""Parser tests — realistic IOS output, no SSH involved."""

from __future__ import annotations

from inventory.collector import (
    _parse_vlan_range,
    apply_trunk_output,
    parse_interfaces_status,
    parse_trunk_allowed,
    parse_version,
    parse_vlans,
)
from models import InterfaceMode, InterfaceStatus, VLANStatus


def _row(port="", name="", status="", vlan="", duplex="", speed="", type_=""):
    return f"{port:<10}{name:<19}{status:<13}{vlan:<11}{duplex:<8}{speed:<6}{type_}"


STATUS_OUTPUT = "\n".join([
    _row("Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type"),
    _row("Gi0/1", "uplink to core", "connected", "trunk", "a-full", "a-1000", "10/100/1000BaseTX"),
    _row("Gi0/2", "", "notconnect", "10", "auto", "auto", "10/100/1000BaseTX"),
    _row("Fa0/5", "finance printer", "connected", "30", "a-full", "a-100", "10/100BaseTX"),
])

TRUNK_OUTPUT = """Port        Mode             Encapsulation  Status        Native vlan
Gi0/1       on               802.1q         trunking      99

Port        Vlans allowed on trunk
Gi0/1       1,10-12,30

Port        Vlans allowed and active in management domain
Gi0/1       1,10,11,12,30

Port        Vlans in spanning tree forwarding state and not pruned
Gi0/1       1,10,11,12,30
"""

VLAN_OUTPUT = """VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/1, Gi0/2
10   MGMT                             active
1002 fddi-default                     act/unsup
"""


def test_parse_interfaces_status_keeps_columns_aligned():
    ifaces = parse_interfaces_status("SW-1", STATUS_OUTPUT)

    assert set(ifaces) == {"Gi0/1", "Gi0/2", "Fa0/5"}
    assert ifaces["Gi0/1"].mode is InterfaceMode.TRUNK
    assert ifaces["Gi0/1"].description == "uplink to core"
    assert ifaces["Gi0/2"].access_vlan == 10
    assert ifaces["Gi0/2"].status is InterfaceStatus.DOWN
    # The Name column contains a space; Vlan must still be read from its column.
    assert ifaces["Fa0/5"].access_vlan == 30
    assert ifaces["Fa0/5"].mode is InterfaceMode.ACCESS
    assert ifaces["Fa0/5"].status is InterfaceStatus.UP


def test_parse_interfaces_status_never_creates_a_phantom_port_entry():
    ifaces = parse_interfaces_status("SW-1", STATUS_OUTPUT)
    assert "Port" not in ifaces


def test_trunk_output_does_not_create_section_headers_as_interfaces():
    ifaces = parse_interfaces_status("SW-1", STATUS_OUTPUT)
    apply_trunk_output("SW-1", TRUNK_OUTPUT, ifaces)

    assert set(ifaces) == {"Gi0/1", "Gi0/2", "Fa0/5"}
    assert ifaces["Gi0/1"].trunk_vlans == [1, 10, 11, 12, 30]
    assert ifaces["Gi0/1"].native_vlan == 99


def test_parse_trunk_allowed_is_a_pure_parser():
    allowed = parse_trunk_allowed(TRUNK_OUTPUT)

    assert allowed == {"Gi0/1": [1, 10, 11, 12, 30]}


def test_parse_trunk_allowed_ignores_section_headers():
    allowed = parse_trunk_allowed(TRUNK_OUTPUT)
    assert "Port" not in allowed
    assert "Vlans" not in allowed


def test_parse_vlans_handles_unsupported_and_names():
    vlans = parse_vlans("SW-1", VLAN_OUTPUT)

    by_id = {vlan.vlan_id: vlan for vlan in vlans}
    assert by_id[10].name == "MGMT"
    assert by_id[10].status is VLANStatus.ACTIVE
    assert by_id[1002].status is VLANStatus.INACTIVE


def test_parse_version_extracts_platform_details():
    output = (
        "Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), "
        "Version 15.0(2)SE11, RELEASE SOFTWARE (fc3)\n"
        "cisco WS-C2960-24TT-L (PowerPC405) processor (revision B0)\n"
        "Processor board ID FOC1234X0YZ\n"
        "Switch uptime is 3 weeks, 2 days, 4 hours, 5 minutes\n"
        "Model number                    : WS-C2960-24TT-L\n"
    )
    parsed = parse_version(output)

    assert parsed["platform"] == "Cisco IOS 15.0(2)SE11"
    assert parsed["model"] == "WS-C2960-24TT-L"
    assert parsed["serial"] == "FOC1234X0YZ"
    assert parsed["uptime"].startswith("3 weeks")


def test_parse_vlan_range_expands_ranges():
    assert _parse_vlan_range("1,10-12,30") == [1, 10, 11, 12, 30]
    assert _parse_vlan_range("garbage") == []
