# R1 handoff: PC-level self-healing (Resilience Service)

**For:** Areej Fatima, originally.
**Note (2026-10-21):** an updated `FYP_Proposal.docx` lists Muhammad Moeez as the sole author
across all of R1-R4 (Table 0, Table 12) - no other names appear in it. This doc's "for Areej"
framing below is historical context for how it was written, not the current team structure;
items 1-3 are now implemented as Moeez's own work (see their entries below for what's verified
vs. not). Issues #1 and #2 predate this and are now both resolved and closed - see item 3's own
note below.
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
  checked every 5s (tightened from 30s as part of item 4 below).
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
  of your own work on this component, the same situation as pool consolidation. Both raised as
  credit questions for the team and now both **resolved and closed**:
  [issue #2](https://github.com/moeezmalik10/crms-deploy/issues/2) (node quarantine, closed
  Oct 20, 2026 - the updated sole-author proposal supersedes the original question, no separate
  teammate's graded work is at stake) and
  [issue #1](https://github.com/moeezmalik10/crms-deploy/issues/1) (pool consolidation, closed
  Oct 19, 2026 - shared credit between R3 and R4). It's yours to review, take ownership of, and
  extend — not a to-do. Details moved to "Already done" status under item 3 itself, below.

**What this means for scope:** the lease/checkpoint mechanics for divisible work, and quarantine,
are built. Your remaining work is the rest of the **PC-level** half: making the agent itself
resilient (service + watchdog + log rotation).

## What's genuinely missing

### 1. Agent as a Windows service with a watchdog — written, needs a real run to verify
`node-agent/install_service.ps1` (+ `install_service.bat`/`uninstall_service.bat`) registers
`agent.py` as a Windows service via NSSM, following `setup_sandbox.ps1`'s exact pattern
(self-elevation, a `Run-Native` helper, transcript logging to `install_service.log`, idempotent
re-run). Configures NSSM's own restart-on-exit as the watchdog (`AppExit Default Restart`,
`AppRestartDelay` 5s, `AppThrottle` 10s so a genuine crash-loop backs off instead of spinning),
auto-start on boot, and NSSM's own rotated stdout/stderr capture as a safety net below the
agent's own logging. `DEPLOY_GUIDE.md` Step 6 now mentions it as the option for a lab PC that
should stay in the pool unattended.

**What's actually verified vs. not**: the script's syntax is checked (PowerShell's own AST
parser) and it mirrors `setup_sandbox.ps1` closely, but it was never executed - installing a
Windows service is a system-level change, not something done without you directly running it
and watching what happens. Before trusting it: run `install_service.bat` on a real PC (you'll
need `nssm.exe` from nssm.cc first - see `node-agent/tools/README.md`), confirm the service
shows **Running** in `services.msc`, kill the agent process and confirm NSSM restarts it, and
reboot the PC to confirm it comes back on its own.

### 2. Log rotation — done, verified
`node-agent/agent.py::setup_logging()` had no file handler at all before this - console-only,
so a service with no visible console would lose all log output. Added a
`logging.handlers.RotatingFileHandler` (stdlib) writing to `node-agent/agent.log`, capped at
5MB × 5 backups (25MB total). Verified directly: a standalone rotation test confirms old
content is dropped once the cap is hit, and the real path (next to `agent.py`) resolves and
writes correctly.

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

### 4. Tighten the detection-to-recovery latency — partially done (the lower-risk half)
The proposal's "resumes... within 60 seconds" had worst-case latency of 90–105s (60s heartbeat
timeout + up to 30s until the next `cleanup_system` tick + up to 15s until the next
`process_queue` tick picks the requeued work back up). Two honest options were on the table:
- Shrink `cleanup_system`'s own tick interval without touching the 60s heartbeat-timeout
  threshold itself — cuts detection latency without touching false-positive tolerance.
- A genuine per-unit lease (not just node-level heartbeat) — the proposal's Component 5 mentions
  this ("processed under a lease by the node agent") as a job-engine concept; a real tradeoff to
  weigh, not something to default into.

**Took the first option, not the second** - it's the one the original framing above already
called the safer default, and it needed no new mechanism, just two numbers. `cleanup_system` and
`process_queue` (`backend/app/tasks.py`) now both tick every 5s (was 30s/15s) - the 60s
heartbeat-timeout threshold is deliberately **unchanged**, so the agent-registration-retry fix's
false-positive protection (`SESSION_SUMMARY.md` §1) isn't reintroduced. Both queries are cheap
(indexed, a handful of rows at this project's scale), so five-second polling has little real
cost. **New worst case: 60 + 5 + 5 = 70s** — reported honestly, not rounded down to "within 60s":
the 60s heartbeat timeout alone already consumes the entire target budget, so hitting the literal
60s figure isn't possible without shrinking that threshold, which this deliberately did not do.
Locked in with `backend/tests/test_r1_latency.py` (asserts both tick intervals directly against
the scheduler's own registered jobs, not just the source) so a future change can't silently widen
the budget back out unnoticed.

**The per-unit lease option is still open** - a bigger, separate mechanism (shorter-timeout
detection at the unit level instead of the node level), genuinely a different tradeoff than what
was built here, not attempted.

## Suggested order (status as of 2026-10-23)
1. Service + watchdog (NSSM) — script written (item 1), **still not yet run on a real PC**. Do
   this first: install `nssm.exe`, run `install_service.bat`, confirm it actually restarts the
   agent on a kill and on reboot. The only item in this whole plan still blocked purely on
   someone running it.
2. ~~Log rotation~~ — done and verified (item 2).
3. ~~Node quarantine~~ — done and verified live (item 3).
4. ~~Detection latency~~ — the tick-interval half done and tested (item 4); the per-unit-lease
   half still open, a separate decision.

## Testing
`backend/tests/test_r1_latency.py` (2 tests) locks in item 4's tick intervals directly against
the scheduler's own registered jobs. `backend/tests/test_node_quarantine.py` (15 tests) covers
node quarantine — counter increment/reset, the stability-window streak logic, scheduling
exclusion, and the admin-UI clear-quarantine endpoints. No existing tests cover the
service/watchdog layer (can't
meaningfully unit-test an actual Windows service in CI). See `backend/tests/test_r3_resilience.py`
for the existing style/patterns to match (fixtures, `make_node`, directly calling
`cleanup_system()`/`allocate_task()` rather than going through HTTP where a test doesn't need to).
