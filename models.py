"""
Data models for NetProvision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class DeviceType(Enum):
    CISCO_IOS   = "cisco_ios"
    CISCO_IOSXE = "cisco_xe"
    CISCO_NXOS  = "cisco_nxos"
    HP_PROCURVE = "hp_procurve"


class InterfaceMode(Enum):
    ACCESS  = "access"
    TRUNK   = "trunk"
    ROUTED  = "routed"
    UNKNOWN = "unknown"


class InterfaceStatus(Enum):
    UP      = "up"
    DOWN    = "down"
    UNKNOWN = "unknown"


class VLANStatus(Enum):
    ACTIVE   = "active"
    INACTIVE = "inactive"


class JobStatus(Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    ROLLEDBACK= "rolledback"


class OperationType(Enum):
    VLAN_CREATE       = "vlan_create"
    VLAN_DELETE       = "vlan_delete"
    TRUNK_ADD_VLAN    = "trunk_add_vlan"
    TRUNK_REMOVE_VLAN = "trunk_remove_vlan"
    ACCESS_SET_VLAN   = "access_set_vlan"
    INTERFACE_REFRESH = "interface_refresh"


# ── Device ────────────────────────────────────────────────────────────────────

@dataclass
class DeviceCredentials:
    hostname:      str
    ip:            str
    device_type:   DeviceType     = DeviceType.CISCO_IOS
    username:      str            = ""
    password:      str            = ""
    enable_secret: str            = ""
    port:          int            = 22
    timeout:       int            = 30
    tags:          dict[str, str] = field(default_factory=dict)


# ── Inventory ─────────────────────────────────────────────────────────────────

@dataclass
class VLANRecord:
    vlan_id:    int
    name:       str
    status:     VLANStatus         = VLANStatus.ACTIVE
    device:     str                = ""
    discovered: Optional[datetime] = None


@dataclass
class InterfaceRecord:
    device:         str
    name:           str             # "GigabitEthernet0/0"
    description:    str             = ""
    mode:           InterfaceMode   = InterfaceMode.UNKNOWN
    status:         InterfaceStatus = InterfaceStatus.UNKNOWN
    access_vlan:    Optional[int]   = None
    trunk_vlans:    list[int]       = field(default_factory=list)
    native_vlan:    Optional[int]   = None
    ip_address:     Optional[str]   = None
    mac_address:    Optional[str]   = None
    speed:          Optional[str]   = None
    duplex:         Optional[str]   = None
    discovered:     Optional[datetime] = None


@dataclass
class DeviceRecord:
    hostname:      str
    ip:            str
    device_type:   str
    platform:      str             = ""   # e.g. "Cisco IOS 15.2"
    serial:        str             = ""
    model:         str             = ""
    uptime:        str             = ""
    tags:          dict[str, str]  = field(default_factory=dict)
    last_seen:     Optional[datetime] = None
    interfaces:    list[InterfaceRecord] = field(default_factory=list)
    vlans:         list[VLANRecord]      = field(default_factory=list)


# ── Provisioning ──────────────────────────────────────────────────────────────

@dataclass
class VLANProvisionRequest:
    """
    API request body for VLAN provisioning.

    Examples:
      Create VLAN 40 on two switches and add to their trunks:
        vlan_id=40, name="Finance", switches=["SW-CORE","SW-ACCESS"],
        add_to_trunks=["SW-CORE:Gi0/1","SW-ACCESS:Gi0/1"]

      Assign VLAN 40 to an access port:
        vlan_id=40, access_ports=["SW-ACCESS:Fa0/5"]
    """
    vlan_id:         int
    name:            str
    switches:        list[str]     = field(default_factory=list)  # hostnames
    add_to_trunks:   list[str]     = field(default_factory=list)  # "hostname:interface"
    access_ports:    list[str]     = field(default_factory=list)  # "hostname:interface"
    description:     str           = ""
    requested_by:    str           = "api"


@dataclass
class OperationStep:
    """One atomic step within a provisioning job."""
    device:         str
    operation:      OperationType
    interface:      Optional[str]
    vlan_id:        int
    vlan_name:      str             = ""
    command:        str             = ""   # the IOS command sent
    output:         str             = ""   # device response
    success:        bool            = False
    error:          Optional[str]   = None
    timestamp:      Optional[datetime] = None
    rollback_cmd:   str             = ""   # command to undo this step
    rollback_note:  str             = ""   # why a rollback is manual / skipped
    rollback_applied: bool          = False
    rollback_error: str             = ""


@dataclass
class ProvisioningJob:
    job_id:      str
    request:     VLANProvisionRequest
    status:      JobStatus              = JobStatus.PENDING
    steps:       list[OperationStep]    = field(default_factory=list)
    created_at:  Optional[datetime]     = None
    completed_at:Optional[datetime]     = None
    error:       Optional[str]          = None
    steps_total: int                    = 0   # known even when steps are not loaded

    @property
    def steps_attempted(self) -> int:
        return sum(1 for s in self.steps if s.timestamp is not None)

    @property
    def steps_succeeded(self) -> int:
        return sum(1 for s in self.steps if s.success)

    @property
    def steps_failed(self) -> int:
        return sum(1 for s in self.steps if s.timestamp is not None and not s.success)
