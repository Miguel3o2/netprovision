# NetProvision — VLAN Provisioning & Network Inventory API

A REST API that automates VLAN provisioning across Cisco switches and maintains
a queryable network inventory. POST a provisioning request and the API SSHs into
each switch, creates the VLAN, adds it to trunks, assigns access ports, and rolls
back cleanly on any failure. All operations are logged with full command-level audit trails.

---

## Architecture

```
config.yaml  (device credentials)
    │
    ▼
main.py
    │
    ├── inventory/collector.py    SSH → show commands → parse → SQLite
    │   inventory/store.py        SQLite: devices, interfaces, VLANs, jobs
    │
    ├── provisioner/engine.py
    │     validate()   — check VLAN ID, names, device existence, duplicates
    │     build_plan() — pure fn: request → list[OperationStep] with IOS commands
    │     execute()    — SSH to devices, send commands, record output
    │     rollback()   — reverse completed steps in reverse order on failure
    │
    └── api/server.py   FastAPI
          POST /api/vlans/provision              → 202 + job_id
          GET  /api/vlans/provision/{job_id}     → job status + step detail
          POST /api/vlans/provision/{id}/rollback
          GET  /api/inventory/devices
          GET  /api/inventory/devices/{h}
          GET  /api/inventory/devices/{h}/interfaces
          GET  /api/inventory/devices/{h}/vlans
          GET  /api/inventory/vlans
          GET  /api/inventory/vlans/{vlan_id}   → where it exists + ports using it
          GET  /api/inventory/interfaces/vlan/{id}
          POST /api/inventory/refresh/{hostname}
          GET  /api/jobs
          GET  /                                  → web dashboard
```

---

## Quick Start

```bash
git clone https://github.com/Miguel3o2/netprovision
cd netprovision
pip install -r requirements.txt

cp configs/config.example.yaml config.yaml
# Edit: set device IPs and credentials

# Enable SSH on Cisco devices:
# SW-CORE(config)# username admin privilege 15 secret cisco123
# SW-CORE(config)# line vty 0 15
# SW-CORE(config-line)# transport input ssh
# SW-CORE(config-line)# login local
# SW-CORE(config)# ip ssh version 2

python main.py --collect   # collect inventory then start API
# or
python main.py             # start API without initial collection

# Dashboard: http://localhost:8002
# API docs:  http://localhost:8002/docs
```

`config.yaml` is git-ignored — credentials stay on your machine. Only
`configs/config.example.yaml` (placeholders) is committed.

### Tests

No SSH, no devices, no pytest required:

```bash
python tests/test_core.py     # 29 tests: validation, plan builder, store
# or: pytest -q
```

The engine tests cover the pure `build_plan()` and `validate()` paths plus the
SQLite store, so they run in well under a second.

---

## Provision a VLAN

```bash
curl -X POST http://localhost:8002/api/vlans/provision \
  -H "Content-Type: application/json" \
  -d '{
    "vlan_id": 40,
    "name": "Finance",
    "switches": ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
    "access_ports": ["SW-ACCESS:Fa0/5"]
  }'

# Response:
# { "job_id": "abc123...", "status": "pending", "steps": 5 }

# Poll status:
curl http://localhost:8002/api/vlans/provision/abc123.../
```

### What that does on the devices

**SW-CORE and SW-ACCESS** (VLAN_CREATE):
```
configure terminal
vlan 40
 name Finance
exit
exit
write memory
```

**SW-CORE:Gi0/1 and SW-ACCESS:Gi0/1** (TRUNK_ADD_VLAN):
```
configure terminal
interface Gi0/1
 switchport trunk allowed vlan add 40
exit
exit
```

**SW-ACCESS:Fa0/5** (ACCESS_SET_VLAN):
```
configure terminal
interface Fa0/5
 switchport mode access
 switchport access vlan 40
exit
exit
```

### Rollback

If any step fails, the API can undo all completed steps:

```bash
curl -X POST http://localhost:8002/api/vlans/provision/{job_id}/rollback
```

Rollback sends `no vlan 40`, `switchport trunk allowed vlan remove 40`,
and `switchport access vlan 1` — in reverse order of the original steps.

---

## Inventory API

```bash
# List all devices
GET /api/inventory/devices

# Get interfaces for a device
GET /api/inventory/devices/SW-CORE/interfaces

# Find all locations where VLAN 20 is configured
GET /api/inventory/vlans/20

# Find all access and trunk ports carrying VLAN 20
GET /api/inventory/interfaces/vlan/20

# Refresh a device's inventory
POST /api/inventory/refresh/SW-CORE
```

---

## Validation rules

Before any SSH connection is made, the provisioner validates:

| Rule | Error |
|---|---|
| VLAN ID 2–4093 | Out of range |
| Not reserved (1, 1002–1005) | Reserved VLAN |
| Name ≤ 32 chars, no spaces | IOS name limit |
| All devices exist in config | Unknown device |
| Trunk/access refs in `hostname:interface` format | Bad format |
| VLAN doesn't already exist on target switch | Duplicate VLAN |

---

## Provisioning steps order

Steps always run in this order to avoid consistency issues:

1. `VLAN_CREATE` on all target switches (VLAN must exist before trunk/access config)
2. `TRUNK_ADD_VLAN` on specified trunk interfaces
3. `ACCESS_SET_VLAN` on specified access ports

---

## Project structure

```
netprovision/
├── main.py                     Entry point — loads config, starts API
├── models.py                   Dataclasses: DeviceCredentials, VLANProvisionRequest, etc.
├── inventory/
│   ├── collector.py            SSH collector — show commands → parse → store
│   └── store.py                SQLite store — devices, interfaces, VLANs, jobs
├── provisioner/
│   └── engine.py               validate(), build_plan(), execute(), rollback()
├── api/
│   └── server.py               FastAPI endpoints
├── web/
│   └── index.html              Self-contained dashboard — inventory + provision form
├── configs/
│   └── config.example.yaml     Example config — copy to config.yaml
├── tests/
│   └── test_core.py            29 tests — validation, plan builder, store
└── requirements.txt
```

---

## Security notes

This is a lab / portfolio project — read before exposing it anywhere:

- **No authentication or authorization** on the API. It can reconfigure network
  devices, so keep it on a management VLAN or behind an authenticating proxy.
- **CORS is open** (`allow_origins=["*"]`) in `api/server.py` for dashboard
  development; restrict it before deploying.
- **Device credentials are plaintext** in `config.yaml` (git-ignored). Use a
  secrets manager or environment variables for anything real.
- **HTTP, not HTTPS** — terminate TLS in front of uvicorn.
- `write memory` is sent on success, so a rollback restores running-config but
  the startup-config was already saved with the new VLAN. Validate against a
  lab first.

---

## CV Bullets

```
NetProvision — VLAN Provisioning & Network Inventory API  |  github.com/Miguel3o2/netprovision

Python · Netmiko · FastAPI · SQLite · IOS automation

• Built a REST API that automates VLAN provisioning across Cisco switches —
  POST a single request and the API SSHs into each device, creates the VLAN,
  adds it to specified trunk interfaces with `switchport trunk allowed vlan add`,
  and assigns access ports, eliminating manual CLI work and copy-paste errors.

• Implemented a two-phase engine: build_plan() is a pure function that converts
  a VLANProvisionRequest into ordered OperationSteps with IOS commands and
  rollback commands — fully testable without SSH — and execute() SSHs to each
  device and runs the plan, recording raw output per step in SQLite.

• Built automatic rollback: on any step failure, the API reverses all completed
  steps in reverse order using pre-computed rollback commands (no vlan, remove
  from trunk, revert access VLAN) and marks the job ROLLEDBACK.

• Maintains a network inventory (devices, interfaces, VLANs) collected via SSH
  show commands and exposed via /api/inventory/vlans/{id} — queryable to find
  every switch and port carrying a given VLAN, used to validate provisioning
  requests before touching any device.
```

---

## Interview explanation

**On the plan/execute separation:**
> "build_plan() has no side effects — it takes a request and returns a list of
> steps with the exact IOS commands and their rollback commands. That means I can
> unit-test every scenario without SSH and without mocking network connections.
> The 29 tests run in under a second. execute() is the only function that
> touches the network, and it just walks the plan."

**On rollback:**
> "Every step records its rollback command when the plan is built — not at
> execution time. `switchport trunk allowed vlan add 40` rolls back to
> `switchport trunk allowed vlan remove 40`. `vlan 40` rolls back to `no vlan 40`.
> If step 4 fails, we reverse steps 3, 2, 1 in that order. The device ends up
> in exactly the state it was in before we started."

**On the inventory:**
> "The inventory isn't just documentation. It's used for validation — before
> the provisioner touches anything, it checks whether the VLAN already exists
> on the target switch. If it does, the request is rejected with a clear error
> instead of letting IOS silently overwrite the name or cause an inconsistency."
