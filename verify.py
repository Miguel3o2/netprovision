"""
Verification and Snapshots
==========================
Collecting an inventory is not the same as trusting it. This module answers two
questions around every change:

  1. What did the device look like before we touched it?  → capture_snapshot()
  2. Does the device actually show the intended state now? → verify_request()

Both only read: they send `show` commands through a caller-supplied `send`
function, so the engine can hold its per-device lock around each command and the
same code is testable without SSH.

The parsers are the ones the inventory collector uses. If verification and
collection disagreed about what `show vlan brief` means, one of them would be
lying; sharing the parser removes that class of bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from inventory.collector import parse_trunk_allowed, parse_vlans
from models import VLANProvisionRequest

Send = Callable[[str, str], str]

ACCESS_VLAN_RE = re.compile(r"Access Mode VLAN:\s*(\d+)")
MAX_SNAPSHOT_CHARS = 8000


@dataclass
class Check:
    """One assertion about post-change device state."""

    device:   str
    check:    str
    expected: str
    actual:   str
    ok:       bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "device":   self.device,
            "check":    self.check,
            "expected": self.expected,
            "actual":   self.actual,
            "ok":       self.ok,
        }


def capture_snapshot(
    request: VLANProvisionRequest,
    send:    Send,
    max_chars: int = MAX_SNAPSHOT_CHARS,
) -> dict[str, Any]:
    """
    Capture the pre-change state that a human or auditor would need to diff.

    Per device: the VLAN table, plus the running-config section of every
    interface the plan touches. Bounded so one giant config cannot bloat the job
    record.
    """
    snapshot: dict[str, Any] = {}

    for host in _hosts(request):
        entry: dict[str, Any] = {"vlan": "", "interfaces": {}}
        try:
            entry["vlan"] = _truncate(send(host, "show vlan brief"), max_chars)
        except Exception as exc:
            entry["error"] = f"show vlan brief: {exc}"

        for iface in _interfaces_for(request, host):
            try:
                entry["interfaces"][iface] = _truncate(
                    send(host, f"show running-config interface {iface}"),
                    max_chars,
                )
            except Exception as exc:
                entry["interfaces"][iface] = f"error: {exc}"

        snapshot[host] = entry

    return snapshot


def verify_request(request: VLANProvisionRequest, send: Send) -> list[Check]:
    """
    Assert the intended end state, one check per meaningful fact:

      - the VLAN exists on every device the plan touched
      - each access port reports the requested access VLAN
      - each trunk reports the VLAN in its allowed list

    A device can accept commands and still not apply them (wrong context, a
    platform quirk, an interface in an error state). This is where that shows up
    before the job is declared successful.
    """
    checks: list[Check] = []
    vlan_id = request.vlan_id

    for host in _hosts(request):
        try:
            vlans = parse_vlans(host, send(host, f"show vlan id {vlan_id}"))
            present = any(vlan.vlan_id == vlan_id for vlan in vlans)
            actual = "present" if present else "missing"
        except Exception as exc:
            present = False
            actual = f"error: {exc}"

        checks.append(Check(
            device   = host,
            check    = "vlan_present",
            expected = f"VLAN {vlan_id}",
            actual   = actual,
            ok       = present,
        ))

    for host, iface in _refs(request.access_ports):
        try:
            text = send(host, f"show interfaces {iface} switchport")
            match = ACCESS_VLAN_RE.search(text)
            actual = match.group(1) if match else "not reported"
        except Exception as exc:
            actual = f"error: {exc}"

        checks.append(Check(
            device   = host,
            check    = f"access_vlan:{iface}",
            expected = str(vlan_id),
            actual   = actual,
            ok       = actual == str(vlan_id),
        ))

    trunk_cache: dict[str, dict[str, list[int]]] = {}
    for host, iface in _refs(request.add_to_trunks):
        if host not in trunk_cache:
            try:
                trunk_cache[host] = parse_trunk_allowed(send(host, "show interfaces trunk"))
            except Exception as exc:
                trunk_cache[host] = {}
                checks.append(Check(
                    device   = host,
                    check    = f"trunk_allows:{iface}",
                    expected = str(vlan_id),
                    actual   = f"error: {exc}",
                    ok       = False,
                ))
                continue

        allowed = trunk_cache[host].get(iface, [])
        checks.append(Check(
            device   = host,
            check    = f"trunk_allows:{iface}",
            expected = str(vlan_id),
            actual   = ",".join(str(v) for v in allowed) or "none",
            ok       = vlan_id in allowed,
        ))

    return checks


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hosts(request: VLANProvisionRequest) -> list[str]:
    hosts = set(request.switches)
    for ref in request.add_to_trunks + request.access_ports:
        parsed = _split_ref(ref)
        if parsed:
            hosts.add(parsed[0])
    return sorted(hosts)


def _interfaces_for(request: VLANProvisionRequest, host: str) -> list[str]:
    names: set[str] = set()
    for ref in request.add_to_trunks + request.access_ports:
        parsed = _split_ref(ref)
        if parsed and parsed[0] == host:
            names.add(parsed[1])
    return sorted(names)


def _refs(refs: list[str]) -> list[tuple[str, str]]:
    parsed = [_split_ref(ref) for ref in refs]
    return [item for item in parsed if item is not None]


def _split_ref(ref: str) -> Optional[tuple[str, str]]:
    if ":" not in ref:
        return None
    host, iface = ref.split(":", 1)
    if not host or not iface:
        return None
    return host, iface


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"
