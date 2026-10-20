# R1 handoff: PC-level self-healing (Resilience Service)

**For:** Areej Fatima
**Written by:** an AI assistant working with Muhammad Moeez on his own R3 scope, at his request, as
a starting point for you — not a substitute for your own work. Everything here is a plan to
review, adjust, and implement yourself; nothing in this file has been coded for you.

**Drafted because:** the session that produced this plan found, while closing out R3, that the
job-level half of lease-based recovery was a real bug (see "Already done" below) and fixed it as
part of R3's own job engine. The PC-level half — the part the updated proposal actually assigns
to you — was never touched, and the plan below picks up from exactly where that work stopped.

---

## What the proposal asks for (Component 7, Objective 4)

> "A PC is only treated as failed after several missed heartbeats... Every running unit and
> session holds a lease; when a lease expires the work returns to the pool and resumes on
> another PC from its last checkpoint. The agent runs as a Windows service with a watchdog and
> log rotation, and PCs that fail repeatedly are temporarily taken out of the pool."

Success criteria (Objective 4 / M3): work from a failed PC resumes elsewhere within 60 seconds;
only the interrupted unit is repeated; false failure alarms reduced by 90%+.

## What's already done (so you don't duplicate it)

- Heartbeat-timeout detection exists: `backend/app/tasks.py::cleanup_system`, 60s timeout,
  checked every 30s.
- Job-mode (divisible) work now correctly requeues when its node drops, even mid-run — this was
  a real bug fixed this session (`backend/app/allocation_engine.py::release_node_tasks`). See
  `backend/tests/test_r3_resilience.py` for the test coverage and the bug writeup in
  `SESSION_SUMMARY.md` §4.
- Per-unit checkpoint records (SHA-256 hash of each unit's output, verified on every read) exist
  — `TaskRequest.result_sha256`, `backend/app/pool_agent_routes.py::job_result`. This is the
  "resumes... from its last checkpoint" half of the sentence above, already built.
- Node-agent registration retry timing was fixed (8 tries over 120s, matching Render's cold-start
  window) — `node-agent/agent.py`.
- **Node quarantine (item 3 below) is now built and live** — at Moeez's explicit request, ahead
  of your own work on this component, the same situation as pool consolidation (see
  [issue #2](https://github.com/moeezmalik10/crms-deploy/issues/2) for the credit discussion,
  parallel to [issue #1](https://github.com/moeezmalik10/crms-deploy/issues/1) for consolidation).
  It's yours to review, take ownership of, and extend — not a to-do. Details moved to "Already
  done" status under item 3 itself, below.

**What this means for scope:** the lease/checkpoint mechanics for divisible work, and quarantine,
are built. Your remaining work is the rest of the **PC-level** half: making the agent itself
resilient (service + watchdog + log rotation).

## What's genuinely missing

### 1. Agent as a Windows service with a watchdog
Right now the agent is started manually via `node-agent/start_agent.bat`
(`venv\Scripts\python.exe agent.py`) — if it crashes, nothing restarts it, and if the PC reboots,
nothing starts it back up. The project's own tools list already names **NSSM** for exactly this
(`backend/requirements.txt`'s tools table isn't it — check `DEPLOY_GUIDE.md` and the proposal's
Table 14: "psutil and NSSM — Node agent metrics and service... runs the agent as a Windows
service with automatic restart").

- Write an NSSM install script (`node-agent/install_service.bat` or similar) that registers
  `agent.py` as a Windows service, with NSSM's own restart-on-exit behavior as the watchdog layer.
- Decide and document the restart policy (NSSM supports throttling so a crash-loop doesn't spin
  forever).
- Update `DEPLOY_GUIDE.md` / the Contribute page's install steps once this exists, so a
  contributing student gets the resilient version by default, not the manual batch-file version.

### 2. Log rotation
`node-agent/agent.py::setup_logging()` — check what it does today; if it's an unbounded file
handler, a service running for weeks will grow that file forever. Use Python's
`logging.handlers.RotatingFileHandler` (stdlib, no new dependency) with a sane size/backup-count.

### 3. Node quarantine after repeated failures — DONE, read before changing anything
The proposal: "PCs that fail repeatedly are temporarily taken out of the pool." Built and live in
production; review this before extending it, the same way Part 1 of the R4 plan asks Maryam to
read `_consolidate` before deciding what to change.

- `Node.consecutive_failures`, `Node.last_failure_at`, `Node.quarantined_until` —
  `backend/app/models.py`, idempotent migration in `backend/app/migrations.py`.
- `backend/app/allocation_engine.py::register_node_failure` increments the counter (called from
  `cleanup_system`'s dead-node loop) and sets `quarantined_until` past a configurable threshold
  (`QUARANTINE_FAILURE_THRESHOLD`, default 3) for a configurable cooldown
  (`QUARANTINE_COOLDOWN_SECONDS`, default 600s).
- `clear_node_failure` (called from `agent_heartbeat`/`register_node`) only forgives the streak
  after a stability window (`QUARANTINE_STABILITY_WINDOW_SECONDS`, default 300s) with no new
  failure — **read the comment on `clear_node_failure` before changing this**: resetting the
  counter on every heartbeat (the first version's design) makes the threshold mathematically
  unreachable, since a node is only ever re-flagged once per distinct outage. This was a real bug,
  found and fixed before it reached production.
- `lock_online_nodes()`/`fitting_device_count()` exclude a quarantined node from scheduling, even
  while it's heartbeating again.
- Admin-UI surfacing is also done: `GET /admin/nodes` and `GET /pool/devices` report
  `quarantined`/`quarantined_until`/`consecutive_failures`; `POST /admin/nodes/<id>/
  clear_quarantine` (admin) and `PATCH /pool/devices/<id>` with `clear_quarantine: true` (admin or
  the device's owner) release a node early; surfaced in `Machines.jsx` and `PoolDashboard.jsx`
  with a badge and a "Clear quarantine" button.
- Tests: `backend/tests/test_node_quarantine.py` (15 tests) — the behavior spec, read it first.
- Verified live: deployed, confirmed clean in Render's logs, and a real temporary test node was
  driven through 3 real outages against production with no errors (`SESSION_SUMMARY.md` §10).

### 4. Tighten the detection-to-recovery latency (shared with R3, worth discussing together)
The proposal's "resumes... within 60 seconds" currently has worst-case latency closer to 90–105s
(60s heartbeat timeout + up to 30s until the next `cleanup_system` tick + up to 15s until the
next `process_queue` tick picks the requeued work back up). Tightening this without reintroducing
false positives (the thing that was *just* fixed — see `SESSION_SUMMARY.md` §1, the agent
registration retry timing bug) needs a real design decision, not a blind timeout reduction. Two
honest options, worth a conversation with R3:
- Shrink `cleanup_system`'s own tick interval (currently 30s) without touching the 60s
  heartbeat-timeout threshold itself — cuts detection latency without touching false-positive
  tolerance.
- A genuine per-unit lease (not just node-level heartbeat) — the proposal's Component 5 mentions
  this ("processed under a lease by the node agent") as a job-engine concept; whether it's worth
  building as a separate, shorter-timeout mechanism is a real tradeoff to weigh, not something to
  default into.

## Suggested order
1. Read `backend/tests/test_node_quarantine.py` and the quarantine code it covers (item 3) —
   you're taking ownership of this, so understand it before anything else.
2. Service + watchdog (NSSM) — immediately useful even standalone, low risk.
3. Log rotation — small, independent, do it alongside #2 since you're already in `agent.py`.
4. Revisit detection latency once the above are in and you can measure real numbers.

## Testing
`backend/tests/test_node_quarantine.py` (15 tests) covers node quarantine — counter
increment/reset, the stability-window streak logic, scheduling exclusion, and the admin-UI
clear-quarantine endpoints. No existing tests cover the service/watchdog layer (can't
meaningfully unit-test an actual Windows service in CI). See `backend/tests/test_r3_resilience.py`
for the existing style/patterns to match (fixtures, `make_node`, directly calling
`cleanup_system()`/`allocate_task()` rather than going through HTTP where a test doesn't need to).
