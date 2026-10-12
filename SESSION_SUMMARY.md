# CRMS — Session Summary

A record of the work done in this session, in order. All changes are committed to `main` and
live in production unless noted otherwise.

## 1. Security and reliability fixes (first pass)

Two rounds of code review (20 findings total, 19 fixed) covering:

- **Pool-device auth bypass** (`pool_security.py`) — any unauthenticated caller could hijack,
  stop, or read status on any task, including setting attacker-controlled remote-desktop
  credentials. Legacy agents now must prove which PC they claim to be.
- **IDS was fully bypassable** by calling the backend directly instead of going through the
  website. Login is now screened server-side (`auth_routes.py` calls the IDS itself).
- **IDOR fixes**: `/tasks/my/<username>` had no auth at all; students could stop/reallocate
  any other student's task; task creation trusted a client-supplied `user_id`.
- **IDS**: `/unblock` and `/blacklisted` required no auth at all; `X-Forwarded-For` was trusted
  unvalidated (now via `ProxyFix`); a PostgREST query-string injection in the attack counter.
- **Data integrity**: `supabase_setup.sql` reset the admin/student1 password on every re-run
  (fixed); `intrusion_logs` was readable by the public Supabase anon key (now proxied through
  an admin-only backend endpoint); added a unique constraint on `device_key_hash`.
- **Reliability**: an ML job used to hang forever if a device dropped mid-job (now fails
  cleanly); `setup_sandbox.ps1` now aborts instead of silently "succeeding" when a
  security-critical step fails; a flaky result-upload no longer mislabels a successful job as
  failed.
- Verified several of these directly against the live deployment (not just locally), including
  confirming the SQLi-bypass fix actually blocks a direct API call that used to get through.

## 2. R3 implementation (Resource Pool Manager, Pooled Scheduler, Batch Job Engine)

Per the FYP proposal (`CRMS_FYP_Proposal.docx`), Muhammad Moeez's (R3) scope:

- **Quotas unified** (`app/quotas.py`, new) — replaced two ad-hoc guards that had drifted
  apart; fixed a real bug where the session guard's status list was missing `"allocated"`.
- **Priority scheduling** — interactive sessions (priority 10) outrank batch jobs (50).
  Submission now defers to the queue instead of allocating immediately when a
  higher-or-equal-priority request is already waiting. Fixed a queue-starvation bug
  (`limit(3)` ordered only by `created_at` could let 3 stuck requests block everything behind
  them forever).
- **Ledger consistency** — `allocate_task`, `spawn_distributed_ml`, and `_storage_targets` now
  all lock their candidate nodes via one shared `lock_online_nodes()` (the latter two took no
  lock before — a real over-allocation race). Fixed a fairness bug where job-mode tasks didn't
  count against a busy node.
- **Metrics history** — a new `NodeMetricsHistory` table (sampled every 5 min, trimmed after 7
  days) plus `GET /pool/ledger` (reserved vs. in-use vs. free per device) and
  `GET /pool/devices/<id>/history`.
- **Parameter sweep** — the first defined batch job type. `kind=sweep` with one parameter set
  per line; each part gets its own arguments; parts write `output/result.json` with a `score`
  field; the group result picks the best-scoring part automatically. Reachable from the
  website via a new "Job type" selector in Jobs.jsx.
- **K-fold cross-validation** — the second defined batch job type. `kind=kfold`: every part
  gets the *whole* dataset (no row-sharding) and reads `CRMS_PART`/`CRMS_PARTS` to hold out its
  own fold; the group result is the **average** score across folds, not the best one. Same
  "Job type" selector in Jobs.jsx, with a folds (k) picker and a required data file.
- **Consolidation** — per the proposal: "if no single PC has room, the system moves divisible
  work elsewhere to make room." A non-divisible session (physical/remote) that can't fit
  anywhere now checks whether evicting some of a device's divisible job work would free
  enough room, and does so. Never evicts another session; physical sessions require full
  device exclusivity (not just a numeric fit); evicted jobs are cleanly requeued elsewhere.
- Fixed the node-agent's registration retry timing (`agent.py`): 3 tries × 15s wasn't enough
  margin for Render's documented ~60s free-tier cold start, so it failed visibly on nearly
  every cold start even though it usually recovered moments later anyway. Now 8× over 120s.

All of the above verified end-to-end against the **live production backend** (not just
locally): spun up real temporary pool devices, submitted real jobs and sessions, confirmed
consolidation genuinely evicts and requeues, then cleaned up afterward.

## 3. Delete-cascade bug fix

Found live while cleaning up test data: `DELETE /admin/users/<id>` and
`DELETE /admin/nodes/<id>` both threw a raw 500 the moment the user/node had any history at
all, instead of a clean delete or a clear error. Fixed both to resolve every foreign key
properly (task history, execution logs, ML results, join codes, pool files still
mid-replication get a clear 409 instead of a crash or a silent storage leak, contributed
devices are orphaned rather than deleted). Verified against the live backend by actually
deleting the leftover test account and devices from the testing above.

## Testing

- `backend/tests/` (pytest, new this session): 38 tests across quotas, scheduler/priority,
  ledger, metrics history, parameter sweep, k-fold cross-validation, consolidation, and the
  delete-cascade fix.
- SQLite doesn't enforce foreign keys by default (unlike Postgres) — added a
  `PRAGMA foreign_keys=ON` connect listener to the test harness so the delete-cascade tests
  actually catch the bug; confirmed by temporarily reverting the fix and watching 7 of 8 tests
  fail with the real `FOREIGN KEY constraint failed` error.
- All 70 backend tests pass (38 new + 32 from the earlier security-fix pass), no regressions.
- Frontend production build verified after every change.

## Known gaps / not done in this session

Per the proposal's own component breakdown, these are explicitly **other team members'
scope** and were intentionally left alone:
- Lease-based fault tolerance / checkpoint-resume (R1 — Areej)
- Explainable SHAP-based intrusion detection on request-level features (R2 — Laiba)
- Secure remote-access gateway (Apache Guacamole) for off-campus sessions (R4 — Maryam)
- Session/VM one-time credentials and pool consolidation for *interactive sessions specifically
  taking over a device holding storage/VM slots* beyond what R3's job-eviction consolidation
  already covers

Also still open:
- `ids/ids/model_loader.py` downloads and unpickles a model from an unauthenticated public
  Google Drive link at startup — a real (if low-likelihood) RCE vector, flagged but not fixed.
- No CI pipeline (nothing runs tests/build automatically on push).
- The platform still runs on Render/Vercel/Supabase, not the proposal's target self-hosted
  campus server (Ubuntu, PostgreSQL, MinIO, Docker, Nginx) — a full infrastructure migration,
  not attempted here.
- Two more batch job types from the proposal's table are not yet built: Random Forest
  training with tree-merging, and batch simulation / compile-and-test jobs.
