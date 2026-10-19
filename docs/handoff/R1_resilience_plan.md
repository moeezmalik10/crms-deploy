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

**What this means for scope:** the lease/checkpoint *mechanics* for divisible work are built. Your
work is the **PC-level** half: making the agent itself resilient (service + watchdog + log
rotation) and teaching the scheduler to stop trusting a PC that keeps failing.

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

### 3. Node quarantine after repeated failures
Not built at all. The proposal: "PCs that fail repeatedly are temporarily taken out of the pool."

- Add columns to `Node` (in `backend/app/models.py`, same idempotent-migration pattern as the
  rest of this project — see `backend/app/migrations.py` for the template): something like
  `consecutive_failures` (int, default 0) and `quarantined_until` (nullable timestamp).
- In `backend/app/tasks.py::cleanup_system`, where a node is currently just marked `offline` on
  heartbeat timeout, increment `consecutive_failures`; reset it to 0 on a successful heartbeat
  (`backend/app/agent_routes.py::agent_heartbeat`). Past some threshold (make it configurable,
  e.g. an env var), set `quarantined_until = now + some cooldown`.
- In `backend/app/allocation_engine.py::lock_online_nodes` (or right after it, in `allocate_task`),
  exclude nodes with `quarantined_until` in the future from scheduling — same filtering style
  already used for `paused` nodes.
- Surface quarantine state on the admin Machines/Resource Pool page (`frontend/Components/
  Admin_Page_Components/Machines.jsx`, `frontend/Components/Pool/PoolDashboard.jsx`) so an admin
  can see *why* a device isn't getting work, and manually clear it early if they trust the PC is
  actually fine again.

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
1. Service + watchdog (NSSM) — immediately useful even standalone, low risk.
2. Log rotation — small, independent, do it alongside #1 since you're already in `agent.py`.
3. Node quarantine — the only piece needing backend schema changes; follow the migration pattern
   in `backend/app/migrations.py` exactly (idempotent `ADD COLUMN IF NOT EXISTS`, non-fatal on
   failure) since that's what's already live in production.
4. Revisit detection latency once 1–3 are in and you can measure real numbers.

## Testing
No existing tests cover node quarantine or the service/watchdog layer (can't meaningfully unit-
test an actual Windows service in CI, but the quarantine *logic* — counter increment/reset,
scheduling exclusion — is plain Python and should get the same pytest treatment as everything
else in `backend/tests/`. See `backend/tests/test_r3_resilience.py` for the existing
style/patterns to match (fixtures, `make_node`, directly calling `cleanup_system()`/
`allocate_task()` rather than going through HTTP where a test doesn't need to).
