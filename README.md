# NetProvision — VLAN Provisioning & Network Inventory API

A REST API that automates VLAN provisioning across Cisco switches and maintains
a queryable network inventory. Dry-run the exact command plan, then apply it:
the API SSHes into each switch, creates the VLAN, adds it to trunks, assigns
access ports, and reverts completed work automatically if any step fails. Every
step is persisted with its command output and rollback state.

---

## Architecture

```
config.yaml  (device credentials)
    │
    ▼
main.py
    │
    ├── transport.py              the only place that opens SSH sessions
    │
    ├── inventory/collector.py    SSH → show commands → parse → SQLite
    │   inventory/store.py        SQLite: devices, interfaces, VLANs, jobs
    │
    ├── provisioner/engine.py
    │     validate()        — VLAN id, name, device, inventory consistency
    │     build_plan()      — pure fn: request → steps + honest rollback cmds
    │     inspect_request() — plan + warnings, reads inventory only (dry run)
    │     execute()         — plan order, per-device locks, auto-rollback
    │     rollback()        — reverse applied steps, restore captured state
    │
    ├── observability.py          structured logs + Prometheus metrics
    │
    └── api/server.py   FastAPI
          POST /api/vlans/plan                    → dry run: commands + warnings
          POST /api/vlans/provision               → 202 + job_id (Idempotency-Key)
          GET  /api/vlans/provision/{job_id}      → job status + step evidence
          POST /api/vlans/provision/{id}/rollback
          GET  /api/inventory/devices
          GET  /api/inventory/devices/{h}
          GET  /api/inventory/devices/{h}/interfaces
          GET  /api/inventory/devices/{h}/vlans
          GET  /api/inventory/vlans
          GET  /api/inventory/vlans/{vlan_id}     → where it exists + ports using it
          GET  /api/inventory/interfaces/vlan/{id}
          POST /api/inventory/refresh/{hostname}
          GET  /api/jobs                          → paginated summaries
          GET  /api/jobs/{job_id}                 → full job detail
          GET  /healthz  /readyz  /metrics
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

### Docker

```bash
cp configs/config.example.yaml config.yaml      # mount target, edit credentials
docker compose up --build
# same endpoints on http://localhost:8002
```

The image runs as an unprivileged user, keeps the SQLite inventory in a named
volume, and exposes a `HEALTHCHECK` against `/healthz`.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NP_CONFIG` | `config.yaml` | Device credentials + API bind settings |
| `NP_DB_PATH` | `inventory.db` | SQLite inventory location |
| `NP_HOST` / `NP_PORT` | `0.0.0.0` / `8002` | Bind overrides |
| `NP_LOG_FORMAT` | `text` | `json` for one-object-per-line logs |
| `NP_LOG_LEVEL` | `INFO` | Standard logging levels |
| `NP_CORS_ORIGINS` | `*` | Comma-separated origins for the dashboard |

### Tests

```bash
pip install -r requirements-dev.txt
pytest -q                 # 80 tests, ~8s, no SSH or devices
python tests/test_core.py # same core suite, no third-party runner needed
```

Coverage is 85% (gate: 80% in CI). The engine is tested end-to-end against a
fake transport (`tests/fakes.py`) that records every command sent, so rollback
order and captured prior state are verified without touching firmware. Parsers
are tested against realistic `show` output, including the column-shift cases
that break naive `split()` parsing.

---

## Provision a VLAN

### 1. Dry run — see exactly what would happen

```bash
curl -X POST http://localhost:8002/api/vlans/plan \
  -H "Content-Type: application/json" \
  -d '{
    "vlan_id": 40,
    "name": "Finance",
    "switches": ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
    "access_ports": ["SW-ACCESS:Fa0/5"]
  }'
```

Returns each step's commands and rollback commands plus warnings, for example
`"SW-ACCESS:Fa0/5 is not in the inventory — the previous access VLAN is unknown,
so this step cannot be rolled back automatically"`. No device is contacted.

### 2. Apply

```bash
curl -X POST http://localhost:8002/api/vlans/provision \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: change-4711" \
  -d '{
    "vlan_id": 40,
    "name": "Finance",
    "switches": ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
    "access_ports": ["SW-ACCESS:Fa0/5"]
  }'

# Response:
# { "job_id": "abc123...", "status": "pending", "steps": 5, "warnings": [...] }

# Poll status (includes per-step command output and rollback state):
curl http://localhost:8002/api/vlans/provision/abc123...
```

`Idempotency-Key` is optional but recommended: repeating the same request with
the same key returns the original job (`200`, `"replayed": true`) instead of
provisioning twice. Jobs targeting the same VLAN on the same device are rejected
while one is already in flight, and each device is mutex-protected so two jobs
never interleave CLI sessions on one switch.

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

Rollback is automatic: if any step fails, `execute()` reverts the steps that
succeeded, newest first, and marks the job `ROLLEDBACK`. The same revert can be
triggered manually for a finished job:

```bash
curl -X POST http://localhost:8002/api/vlans/provision/{job_id}/rollback
```

Rollback is plan-time, not guesswork. At plan time the engine captures the
interface's current state from the inventory and computes the exact inverse:

| Original step | Rollback command |
|---|---|
| `vlan 40` | `no vlan 40` |
| `switchport trunk allowed vlan add 40` | `switchport trunk allowed vlan remove 40` |
| `switchport access vlan 40` (port was on VLAN 20) | `switchport access vlan 20` |

When the previous state is unknown — or when reverting would destroy config that
existed before the job (the trunk already allowed VLAN 40) — the step gets **no**
rollback command and a `rollback_note` instead. Blindly writing `switchport
access vlan 1`, as the first version did, is how you take a port off a working
VLAN. Steps that cannot be reverted are listed in the job detail for manual
follow-up, rather than silently "succeeding".

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
| VLAN ID 2–4094 | Out of range |
| Not reserved (1, 1002–1005) | Reserved VLAN |
| Name ≤ 32 chars, no spaces | IOS name limit |
| All devices exist in config | Unknown device |
| Trunk/access refs in `hostname:interface` format | Bad format |
| At least one switch, trunk or access port | No targets |
| VLAN doesn't already exist on target switch | Duplicate VLAN |
| Trunk/access host not in `switches` already has the VLAN | Missing VLAN |

---

## Provisioning steps order

Steps always run in this order to avoid consistency issues:

1. `VLAN_CREATE` on all target switches (VLAN must exist before trunk/access config)
2. `TRUNK_ADD_VLAN` on specified trunk interfaces
3. `ACCESS_SET_VLAN` on specified access ports

`execute()` walks the plan in exactly that order (not grouped per device), and
persists each step to SQLite as it completes, so a crash cannot lose evidence for
steps that already ran.

---

## Project structure

```
netprovision/
├── main.py                     Entry point — logging, config, API server
├── models.py                   Dataclasses: DeviceCredentials, ProvisioningJob, ...
├── transport.py                The only module that opens SSH sessions
├── observability.py            JSON logging + Prometheus metrics registry
├── inventory/
│   ├── collector.py            SSH collector — show commands → parse → store
│   └── store.py                SQLite store — devices, interfaces, VLANs, jobs
├── provisioner/
│   └── engine.py               validate(), build_plan(), execute(), rollback()
├── api/
│   └── server.py               FastAPI endpoints, health, metrics
├── web/
│   └── index.html              Self-contained dashboard — inventory + provision form
├── configs/
│   └── config.example.yaml     Example config — copy to config.yaml
├── tests/
│   ├── test_core.py            Core suite, runnable without pytest
│   ├── test_engine_execute.py  Execute/rollback against a fake transport
│   ├── test_collector_parsing.py  Column-aware show-output parsers
│   ├── test_store_jobs.py      Migrations, idempotency, pagination
│   ├── test_api.py             HTTP contract (TestClient)
│   └── fakes.py                Fake connection that records commands
├── Dockerfile / docker-compose.yml
├── pyproject.toml              ruff, mypy, pytest config
├── requirements.txt            Runtime dependencies
└── requirements-dev.txt        Test/lint/audit tooling
```

---

## Security notes

This is a lab / portfolio project — read before exposing it anywhere:

- **No authentication or authorization** on the API. It can reconfigure network
  devices, so keep it on a management VLAN or behind an authenticating proxy.
  (OIDC + RBAC is the next milestone; until then there is no per-user audit.)
- **CORS defaults to open** for dashboard development. Set `NP_CORS_ORIGINS` to a
  comma-separated allowlist before deploying.
- **Device credentials are plaintext** in `config.yaml` (git-ignored). Use a
  secrets manager or environment variables for anything real.
- **HTTP, not HTTPS** — terminate TLS in front of uvicorn.
- Both apply and rollback finish with `write memory`, so a normal failure is
  reverted in startup-config too. A process crash *between* a step and its
  rollback still leaves the running/startup config ahead of the intended state —
  config snapshot + post-change verification is the next milestone.
- **Single process only**: the job queue is FastAPI `BackgroundTasks`, and the
  device/VLAN locks are in-process. Do not run multiple uvicorn workers yet.

---

## CV Bullets

```
NetProvision — VLAN Provisioning & Network Inventory API  |  github.com/Miguel3o2/netprovision

Python · Netmiko · FastAPI · SQLite · IOS automation

• Built a REST API that automates VLAN provisioning across Cisco switches —
  POST a single request and the API SSHes into each device, creates the VLAN,
  adds it to specified trunk interfaces with `switchport trunk allowed vlan add`,
  and assigns access ports, eliminating manual CLI work and copy-paste errors.

• Implemented a two-phase engine: build_plan() is a pure function that converts
  a request into ordered OperationSteps with IOS commands and inverse commands —
  fully testable without SSH — and execute() runs the plan in order, persisting
  every step's command output and rollback state to SQLite as evidence.

• Built automatic rollback from captured prior state: on any step failure the
  engine reverses completed steps newest-first, restoring the port's previous
  access VLAN (not a hard-coded VLAN 1) and refusing to revert config that
  existed before the job, flagging those steps for manual follow-up instead.

• Added a dry-run endpoint that returns the exact command plan and inventory
  warnings without touching a device, plus idempotency keys, per-device locks,
  and VLAN claims so retries and concurrent jobs cannot double-provision.

• Maintains a network inventory (devices, interfaces, VLANs) collected via
  column-aware parsers over `show` output and exposed via /api/inventory — used
  to validate requests and compute honest rollback commands before any device
  is contacted.
```

---

## Interview explanation

**On the plan/execute separation:**
> "build_plan() has no side effects — it takes a request and returns a list of
> steps with the exact IOS commands and their inverse commands. That means I can
> test every scenario without SSH. The 80 tests run in about eight seconds, and
> the failure paths are covered with a fake transport that records what would
> have been sent. execute() is the only function that touches the network, and it
> just walks the plan."

**On rollback:**
> "Every step's inverse is computed at plan time, from the inventory snapshot —
> not guessed at execution time. `switchport trunk allowed vlan add 40` inverts to
> `remove 40`, and an access port that was on VLAN 20 goes back to VLAN 20,
> because we captured that. If we don't know the previous state, or reverting
> would delete config that predates the job, we don't send anything: the step is
> marked as needing a manual revert. A rollback that silently writes VLAN 1 is
> worse than no rollback."

**On the inventory:**
> "The inventory isn't just documentation. It's used for validation — before
> the provisioner touches anything, it checks whether the VLAN already exists
> on the target switch. If it does, the request is rejected with a clear error
> instead of letting IOS silently overwrite the name or cause an inconsistency."
