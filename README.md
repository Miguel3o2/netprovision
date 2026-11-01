# NetProvision — VLAN Provisioning & Network Inventory API

A REST API that automates VLAN provisioning across Cisco switches and maintains
a queryable network inventory. Dry-run the exact command plan, then apply it
(optionally behind an approval gate): the API SSHes into each switch, creates the
VLAN, adds it to trunks, assigns access ports, verifies the result on the device,
and reverts completed work automatically if any step — or the verification —
fails. Every step is persisted with its command output, pre-change snapshot, and
rollback state.

Access is role-based (`viewer`, `operator`, `approver`, `admin`), and the
requester of a change cannot be the person who approves it.

---

## Architecture

```
config.yaml  (device credentials)
    │
    ▼
main.py  (API)                         worker.py  (durable queue, optional)
    │                                     │
    ├── transport.py              the only place that opens SSH sessions
    │                                     │
    ├── auth.py                   API keys → roles → permissions
    │                                     │
    ├── inventory/collector.py    SSH → show commands → parse → SQLite
    │   inventory/store.py        SQLite: devices, interfaces, VLANs, jobs,
    │                             claims, leases, deferred rollbacks
    │                                     │
    ├── provisioner/engine.py
    │     validate()        — VLAN id, name, device, inventory consistency
    │     build_plan()      — pure fn: request → steps + honest rollback cmds
    │     inspect_request() — plan + warnings, reads inventory only (dry run)
    │     execute()         — snapshot, plan order, per-device locks, verify, rollback
    │     rollback()        — reverse applied steps, restore captured state
    │                                     │
    ├── verify.py                 pre-change snapshots + post-change assertions
    │                                     │
    ├── observability.py          structured logs + Prometheus metrics
    │                                     │
    └── api/server.py   FastAPI
          POST /api/vlans/plan                    → dry run: commands + warnings
          POST /api/vlans/provision               → 202 + job_id (Idempotency-Key)
          GET  /api/vlans/provision/{job_id}      → status, steps, snapshot, checks
          POST /api/vlans/provision/{id}/approve  → approver releases the job
          POST /api/vlans/provision/{id}/reject   → approver cancels it
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

The API and the worker share SQLite and nothing else. In `NP_QUEUE_MODE=worker`
the API only writes a `pending` job and returns; the worker claims it with a
lease, executes it, and writes each step back. The default `inline` mode keeps
the single-process experience (FastAPI `BackgroundTasks`) for a lab.

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

# Authenticate the API (recommended — see "Authentication & roles" below)
export NP_AUTH_MODE=apikey
export NP_API_KEYS="alice:REPLACE_WITH_RANDOM_TOKEN:operator,bob:REPLACE_WITH_ANOTHER:approver"
# Optional: require a second person to approve every change
export NP_REQUIRE_APPROVAL=true

python main.py --collect   # collect inventory, then start the API (inline mode)

# Optional, but recommended outside a lab: the API only enqueues and a
# separate worker owns device access and survives API restarts.
export NP_QUEUE_MODE=worker
python main.py             # terminal 1
python worker.py           # terminal 2 (or: python main.py --worker)

# Dashboard: http://localhost:8002
# API docs:  http://localhost:8002/docs
```

`config.yaml` is git-ignored — credentials stay on your machine. Only
`configs/config.example.yaml` (placeholders) is committed.

### Docker

```bash
cp configs/config.example.yaml config.yaml      # mount target, edit credentials
export NP_AUTH_MODE=apikey                      # compose passes these through
export NP_API_KEYS="alice:<token>:operator,bob:<token>:approver"
docker compose up --build
# same endpoints on http://localhost:8002
```

The image runs as an unprivileged user, keeps the SQLite inventory in a named
volume, and exposes a `HEALTHCHECK` against `/healthz`. Compose starts two
services: `api` and `worker`. The API defaults to `NP_QUEUE_MODE=worker` there,
so a request is persisted and executed by the worker container. If you start
only `api` (`docker compose up api`), jobs stay `pending` until a worker runs —
that is the correct signal, not a lost job.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NP_CONFIG` | `config.yaml` | Device credentials + API bind settings |
| `NP_DB_PATH` | `inventory.db` | SQLite inventory location |
| `NP_HOST` / `NP_PORT` | `0.0.0.0` / `8002` | Bind overrides |
| `NP_AUTH_MODE` | `off` | `off` (lab) or `apikey` |
| `NP_API_KEYS` | — | `name:token:role` entries, comma-separated |
| `NP_REQUIRE_APPROVAL` | `false` | New jobs wait as `pending_approval` |
| `NP_SEPARATION_OF_DUTIES` | `true` | Requester may not approve their own job |
| `NP_QUEUE_MODE` | `inline` | `inline` (BackgroundTasks) or `worker` (durable queue) |
| `NP_WORKER_ID` | `hostname:pid` | Worker identity written to the job lease |
| `NP_LEASE_SECONDS` | `300` | Lease lifetime; heartbeat runs every third of it |
| `NP_WORKER_POLL_SECONDS` | `2` | Idle poll interval for the worker |
| `NP_WORKER_RECLAIM` | `true` | Close out jobs whose worker lease expired |
| `NP_LOG_FORMAT` | `text` | `json` for one-object-per-line logs |
| `NP_LOG_LEVEL` | `INFO` | Standard logging levels |
| `NP_CORS_ORIGINS` | `*` | Comma-separated origins for the dashboard |

Invalid auth configuration fails at startup rather than silently running open:
`apikey` mode without keys, an unknown role, or a malformed entry is a hard
error.

### Authentication & roles

Keys are compared in constant time (`hmac.compare_digest`). Generate high-entropy
tokens, one per person — never a shared key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Roles are permission sets, not a hierarchy:

| Role | Permissions | Can do |
|---|---|---|
| `viewer` | `read` | Inspect inventory and jobs, dry-run plans |
| `operator` | `read`, `write` | Plan, provision, refresh inventory, roll back |
| `approver` | `read`, `approve` | Approve or reject pending jobs |
| `admin` | `read`, `write`, `approve` | Everything |

When authentication is enabled, `requested_by` is taken from the authenticated
principal and the client-supplied value is ignored — otherwise separation of
duties would be trivially spoofable. The configured `name` is the identity used
for that check: rotate a person's token under the same name, never introduce a
second name for the same human. The dashboard has an `API token` field in the
top bar; it is stored in that browser's `localStorage` only. `GET /healthz`,
`/readyz` and `/metrics` stay unauthenticated for probes and scraping.

### Tests

```bash
pip install -r requirements-dev.txt
pytest -q                 # 137 tests, ~4s, no SSH or devices
python tests/test_core.py # 36 core tests, no third-party runner needed
```

Coverage is 84% (gate: 80% in CI). The engine is tested end-to-end against a
fake transport (`tests/fakes.py`) that simulates IOS state — VLAN table, access
VLANs, trunk allowed lists — so apply, verification, and rollback are exercised
as one loop without touching firmware. Parsers are tested against realistic
`show` output, including the column-shift cases that break naive `split()`
parsing, and the fake can be told to silently ignore config to prove that
verification catches a device that accepted commands but did not apply them.

---

## Provision a VLAN

### 1. Dry run — see exactly what would happen

```bash
curl -X POST http://localhost:8002/api/vlans/plan \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NP_TOKEN" \
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
  -H "Authorization: Bearer $NP_TOKEN" \
  -H "Idempotency-Key: change-4711" \
  -d '{
    "vlan_id": 40,
    "name": "Finance",
    "switches": ["SW-CORE", "SW-ACCESS"],
    "add_to_trunks": ["SW-CORE:Gi0/1", "SW-ACCESS:Gi0/1"],
    "access_ports": ["SW-ACCESS:Fa0/5"]
  }'

# Response (no approval workflow):
# { "job_id": "abc123...", "status": "pending", "steps": 5, "warnings": [...] }

# Response (NP_REQUIRE_APPROVAL=true):
# { "job_id": "abc123...", "status": "pending_approval", "steps": 5,
#   "approval_required": true, "message": "queued for approval ..." }

# Poll status (includes per-step command output, snapshot, verification):
curl http://localhost:8002/api/vlans/provision/abc123... \
  -H "Authorization: Bearer $NP_TOKEN"
```

`Idempotency-Key` is optional but recommended: repeating the same request with
the same key returns the original job (`200`, `"replayed": true`) instead of
provisioning twice. Jobs targeting the same VLAN on the same device are rejected
while one is already in flight, and each device is mutex-protected so two jobs
never interleave CLI sessions on one switch.

### Approval workflow

Set `NP_REQUIRE_APPROVAL=true` and a provisioning request stops at
`pending_approval`. Nothing is sent to a device; the plan is built and stored
with its warnings so the approver sees exactly what would run.

```bash
# A different person with the `approver` (or `admin`) role releases it
curl -X POST http://localhost:8002/api/vlans/provision/abc123.../approve \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"comment": "CAB-4711 approved"}'

# Or rejects it — the job becomes `cancelled`, with the reason in `error`
curl -X POST http://localhost:8002/api/vlans/provision/abc123.../reject \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"comment": "wrong VLAN for this site"}'
```

With `NP_SEPARATION_OF_DUTIES=true` (the default when approval is enabled), the
authenticated requester cannot approve their own job (`409`). Approving a job
that is not `pending_approval` is also `409`; approving without the permission is
`403`; an unauthenticated call is `401`. The job records `approved_by`,
`approved_at`, and the approver's comment, and exposes them on
`GET /api/vlans/provision/{job_id}` for the audit trail. Approval only queues the
job; the approver's name is not a substitute for the requester's evidence.

### Queue & workers

`NP_QUEUE_MODE` decides who executes a job:

| Mode | Behaviour | Use |
|---|---|---|
| `inline` (default) | The API persists the job, then runs it in FastAPI `BackgroundTasks`. | Lab, tests, single process. |
| `worker` | The API persists the job as `pending` and returns. `python worker.py` claims and executes it. | Anything you would leave running. |

Both modes share the same SQLite store. A claim is a conditional update under
SQLite's write lock (`BEGIN IMMEDIATE`) plus a lease, so two workers cannot claim
the same job; the loser simply gets the next one. Claims increment `attempts` and
set `lease_owner`/`lease_expires_at`; a heartbeat thread renews the lease every
third of `NP_LEASE_SECONDS` while the job runs, and the lease is cleared when it
finishes. `GET /api/vlans/provision/{job_id}` exposes `attempts`, `lease_owner`,
and the rollback request fields.

```bash
python worker.py                 # poll forever, Ctrl-C finishes the current job
python worker.py --once          # drain the queue and exit (cron / smoke test)
python main.py --worker          # same worker, reusing main.py config flags
```

**Crash recovery is deliberately conservative.** If a worker dies, its lease
expires. The next pass marks the job `failed` with an error naming the dead
worker and pointing at the stored snapshot and steps. It does **not** replay the
job automatically and does **not** auto-rollback, because a lease expiry cannot
tell "crashed" from "partitioned": the original worker might still be alive and
mid-change. Two writers on one switch is worse than a human reviewing the
evidence. Once the state is verified, the normal rollback endpoint reverts the
recorded steps.

In worker mode, `POST .../rollback` records `rollback_requested_at` and
`rollback_requested_by` instead of touching a device. The worker claims that
request under the same lease rules and runs `rollback()`; a crash mid-rollback
leaves a stale lease which is cleared on the next pass, and retry is safe because
steps already reverted are marked `rollback_applied`.

Queue state is observable without opening SQLite: `/readyz` reports `queue_mode`
and `jobs_pending`, and `/metrics` exposes `np_jobs_pending`,
`np_jobs_claimed_total`, `np_rollbacks_claimed_total`, and
`np_jobs_reclaimed_total`.

**Limits, stated plainly.** Run exactly one worker process. Per-device locks and
VLAN claims are still in-process, so two workers could target the same switch
with two different jobs even though they would never claim the same job. SQLite
in WAL mode is fine for one host and a handful of processes; it is not a
distributed queue. Postgres plus distributed device locks are the next step, not
something this README claims today.

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

In `NP_QUEUE_MODE=worker` this records a rollback request and the worker executes
it, so the API process never opens an SSH session. A second request while one is
outstanding is `409`.

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

## Verification & snapshots

`send_config_set` returning without an exception is not evidence that the device
applied anything. A command can be accepted in the wrong context, silently
ignored by a platform quirk, or fail to stick on an interface in an error state.
So `execute()` does two extra things:

**Before the first change** it snapshots the objects it is about to touch, per
device: `show vlan brief` and `show running-config interface <iface>` for every
trunk/access interface in the plan. Output is truncated at 8 KB per command and
stored in `job.snapshot`, returned by the job detail endpoint. This is the diff a
human needs afterwards, without shelling into the switch.

**After the last change** it asserts the intended end state (on by default):

| Check | Command | Passes when |
|---|---|---|
| `vlan_present` | `show vlan id 40` | VLAN 40 appears in the VLAN table |
| `access_vlan:<if>` | `show interfaces Fa0/5 switchport` | `Access Mode VLAN: 40` |
| `trunk_allows:<if>` | `show interfaces trunk` | 40 is in the allowed list |

These use the same parsers as the inventory collector (`parse_vlans`,
`parse_trunk_allowed`), so collection and verification cannot disagree about what
the output means. Checks are recorded on the job (`job.verification`,
`verified: true|false`).

If any check fails, the job is marked failed with
`post-change verification failed: ...`, `write memory` is **not** sent, and the
automatic rollback runs. The verification is persisted before rollback, so the
evidence survives the revert. `execute(..., verify_after_apply=False)` exists for
diagnostic runs; tests use it to demonstrate exactly the silent-misconfiguration
case the default catches.

Snapshotting and verification are read-only, but they are not free: one
`show vlan brief` per device plus one `show running-config interface` per touched
interface before the change, then one `show vlan id` per device, one
`show interfaces <if> switchport` per access port, and one
`show interfaces trunk` per device afterwards. On a 50-switch plan that is a few
seconds of CLI time against a change that is about to take far longer to review.

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
4. Verification on every touched device — `write memory` only runs if it passes

`execute()` walks the plan in exactly that order (not grouped per device), and
persists each step to SQLite as it completes, so a crash cannot lose evidence for
steps that already ran.

---

## Project structure

```
netprovision/
├── main.py                     Entry point — logging, config, API server
├── worker.py                   Durable queue worker — claims, leases, recovery
├── models.py                   Dataclasses: DeviceCredentials, ProvisioningJob, ...
├── transport.py                The only module that opens SSH sessions
├── observability.py            JSON logging + Prometheus metrics registry
├── auth.py                     API keys → roles → permissions
├── verify.py                   Pre-change snapshots + post-change assertions
├── inventory/
│   ├── collector.py            SSH collector — show commands → parse → store
│   └── store.py                SQLite store — devices, interfaces, VLANs, jobs
├── provisioner/
│   └── engine.py               validate(), build_plan(), execute(), rollback()
├── api/
│   └── server.py               FastAPI endpoints, auth dependencies, health, metrics
├── web/
│   └── index.html              Self-contained dashboard — inventory + provision form
├── configs/
│   └── config.example.yaml     Example config — copy to config.yaml
├── tests/
│   ├── test_core.py            Core suite, runnable without pytest
│   ├── test_engine_execute.py  Execute/rollback against a fake transport
│   ├── test_auth.py            API keys, roles, 401/403, requested_by
│   ├── test_approval.py        Approval, rejection, separation of duties
│   ├── test_verification.py    Snapshots, post-change checks, silent failure
│   ├── test_collector_parsing.py  Column-aware show-output parsers
│   ├── test_store_jobs.py      Migrations, idempotency, pagination
│   ├── test_queue.py           Claims, leases, recovery, worker-mode dispatch
│   ├── test_api.py             HTTP contract (TestClient)
│   └── fakes.py                IOS simulator used by the engine tests
├── Dockerfile / docker-compose.yml
├── pyproject.toml              ruff, mypy, pytest config
├── requirements.txt            Runtime dependencies
└── requirements-dev.txt        Test/lint/audit tooling
```

---

## Security notes

This is a lab / portfolio project — read before exposing it anywhere:

- **Authentication is API keys, not OIDC.** `NP_AUTH_MODE=apikey` gives each
  person a token and a role, with constant-time comparison and a `requested_by`
  identity that cannot be spoofed in the body. It is deliberately not pretending
  to be SSO: there is no token expiry/rotation, no JWT validation, and no per-VLAN
  scoping. OIDC/JWKS is the next milestone, and `auth.authenticate()` is the
  single function it needs to replace.
- **Without `NP_AUTH_MODE=apikey` the API is unauthenticated** — the startup log
  warns loudly. Never expose that mode outside a lab.
- **CORS defaults to open** for dashboard development. Set `NP_CORS_ORIGINS` to a
  comma-separated allowlist before deploying.
- **Device credentials are plaintext** in `config.yaml` (git-ignored). Use a
  secrets manager or environment variables for anything real.
- **HTTP, not HTTPS** — terminate TLS in front of uvicorn.
- Both apply and rollback finish with `write memory`, so a normal failure is
  reverted in startup-config too. Post-change verification catches a device that
  silently ignored commands, but a process crash *between* the last applied step
  and the rollback still leaves the running/startup config ahead of the intended
  state. The stored pre-change snapshot is what makes that recoverable. Worker
  mode does not change this: an expired lease marks the job failed for review
  rather than guessing, because the original worker might still be alive.
- **Run one worker process.** The durable lease prevents two workers claiming the
  same *job* and lets the API restart without losing queued work, but the
  per-device locks and VLAN claims are still in-process: two workers could target
  the same switch with two different jobs. SQLite + WAL is single-host; do not put
  it on NFS. Horizontal scaling needs Postgres and distributed device locks.
  Inline mode keeps FastAPI `BackgroundTasks` and must not run under multiple
  uvicorn workers either.
- **Approval is workflow evidence, not a security boundary against a malicious
  admin.** An `admin` can perform both `write` and `approve`; separation of
  duties prevents the common case (same person approving their own change), not
  collusion.

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

• Added "verify, don't trust" execution: before applying, the engine snapshots
  the VLAN table and the running-config section of every touched interface;
  afterwards it re-reads the device (`show vlan id`, `show interfaces switchport`,
  `show interfaces trunk`) and fails + rolls back the job if the intended state
  is not actually present.

• Added API-key authentication with role-based permissions (viewer / operator /
  approver / admin), constant-time key comparison, and an approval workflow with
  separation of duties: when enabled, a change waits as `pending_approval` until
  a different authenticated person releases it, with approver, timestamp, and
  comment stored on the job.

• Added a durable SQLite-backed job queue alongside the inline path: atomic
  claims, per-job leases renewed by a heartbeat thread, crash recovery for
  expired leases, and a separate worker process, so an API restart no longer
  strands approved work. Rollbacks are queued through the same path.

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
> test every scenario without SSH. The 137 tests run in about four seconds, and
> the failure paths are covered with a fake transport that simulates IOS state.
> execute() is the only function that touches the network, and it just walks the
> plan."

**On rollback:**
> "Every step's inverse is computed at plan time, from the inventory snapshot —
> not guessed at execution time. `switchport trunk allowed vlan add 40` inverts to
> `remove 40`, and an access port that was on VLAN 20 goes back to VLAN 20,
> because we captured that. If we don't know the previous state, or reverting
> would delete config that predates the job, we don't send anything: the step is
> marked as needing a manual revert. A rollback that silently writes VLAN 1 is
> worse than no rollback."

**On verification:**
> "The thing that bit me in review was treating `send_config_set` not raising as
> proof the change landed. It isn't. So after the last step I read the device
> back: `show vlan id 40` on every switch, the switchport state on each access
> port, and the allowed list on each trunk. I reuse the inventory parsers, so
> there's one definition of 'VLAN present' and 'trunk allows 40'. If any check
> fails, the job is failed, startup-config is not saved, and the rollback runs —
> and the check results are persisted before the rollback so the evidence is
> still there. I have a test with a device that accepts every command and applies
> none of them; without verification it reports success."

**On the approval workflow:**
> "The requester and the approver have different permissions: operators have
> `write`, approvers have `approve`, and with separation of duties on, the
> authenticated requester can't approve their own job. I take `requested_by`
> from the token rather than the body, because otherwise a caller can just claim
> to be someone else and the whole check is theatre. The approval stores who,
> when, and the change-ticket comment on the job."

**On the inventory:**
> "The inventory isn't just documentation. It's used for validation — before
> the provisioner touches anything, it checks whether the VLAN already exists
> on the target switch. If it does, the request is rejected with a clear error
> instead of letting IOS silently overwrite the name or cause an inconsistency."
