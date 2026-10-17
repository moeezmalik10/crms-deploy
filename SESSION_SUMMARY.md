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

## 4. Updated proposal alignment (R3 Phase 2)

Partway through the session the team's proposal was substantially rewritten — more rigorous,
measurable success criteria, and a target architecture (self-hosted campus server, six Docker
microservices behind Nginx, MinIO, Apache Guacamole) very different from what's actually
deployed (Render/Vercel/Supabase, a monolithic Flask backend + a separate IDS service). That
infrastructure gap is a whole-team, deployment-target decision and was deliberately not acted
on here. What follows is what *is* R3's job under the new proposal, reviewed point by point
against the actual code and then closed:

- **Unified Quota Ledger** — the proposal names this as a literal deliverable (Component 3):
  persisted `Node.used_cores` / `reserved_cores` / `used_ram_mb` / `reserved_ram_mb`, computed
  by one shared function (`compute_node_usage`) that both the scheduler's scoring loop and
  `GET /pool/ledger` now call — previously `/pool/ledger` had its own separate inline
  computation of the same used/reserved split, a duplicate-logic drift risk this removes. A
  Postgres `CHECK` constraint guards `used + reserved <= total`, declared on the model itself
  (not only in the live migration) so the SQLite test database enforces it too — confirmed it
  actually fires by writing a test that deliberately violates it. Physical sessions now book a
  device's *entire* capacity in the ledger (Rule A gives them the whole PC), not just their own
  modest profile, which the old `/pool/ledger` endpoint got wrong.
- **Deterministic lock ordering + closed a quota race** — `lock_online_nodes()` now locks
  nodes `ORDER BY id`, preventing two concurrent allocators from deadlocking by acquiring the
  same nodes in opposite order. `app/quotas.py` previously did a plain unlocked `COUNT()` —
  two submissions from the same student arriving together could both read "under the limit"
  and both pass. Now locks the student's own `User` row first.
- **Job-mode fault recovery, a real correctness bug** — `release_node_tasks()` used to mark a
  job *already running* when its node dropped as permanently `failed`, the same as a session,
  directly contradicting "work resumes elsewhere within 60s." Divisible job-mode work is
  stateless and safely reruns from scratch wherever it lands next, so it now requeues
  regardless of status; sessions and ML federated rounds still fail outright (correctly —
  they're stateful and can't silently resume elsewhere). Verified the existing
  `job_result()` endpoint already rejects a late result from a since-reassigned stale node (a
  false-positive offline detection can't double-apply a result).
- **Fixed a ping-pong bug the above would have made routine** — the "stuck in starting" sweep
  in `app/tasks.py` keyed off `created_at`, which never moves across requeues, so any job older
  than 5 minutes got bounced straight back to pending the instant it next reached "starting."
  Added `TaskRequest.allocated_at`, set fresh on every (re-)allocation, and keyed the sweep off
  that instead; also extended it to cover `allocated` (previously had no timeout at all if an
  agent never polled).
- **Free-memory-based unit sizing** — for the plain split job path only (`kind=""`); sweep,
  k-fold, RF, and compile-and-test are left alone since their part count means something
  specific (one parameter set, one fold, one tree group, a slice of test cases), not a capacity
  choice. `fitting_device_count()` reads the ledger and clamps requested parts down to however
  many devices can currently actually take one, per the proposal's "units sized from each PC's
  free memory."
- **Random Forest training** (`kind=rf`) — third defined batch job type. Like k-fold, every
  part gets the whole dataset and reads `CRMS_PART`/`CRMS_PARTS` to pick its own random seed;
  writes `output/result.json` with a `trees` field. Combine step concatenates every part's
  trees into one merged forest. Deliberately a JSON array, not a pickled model — unpickling a
  student's upload server-side would be a straightforward RCE hole.
- **Batch compile-and-test** (`kind=compile_test`) — fourth and last defined batch job type.
  Unlike k-fold/RF, test cases are *row-sliced* across parts (same mechanism as the plain split
  job) — the point is running a fixed test suite faster, not training on the whole thing in
  every part. Each part writes `passed`/`failed` counts and a `tests` array; combine step sums
  counts and concatenates the array. All six job types from the proposal's table are now
  covered (chunked data processing was already satisfied by the generic split path).
- **Per-unit checkpoint records** — `TaskRequest.result_sha256`, computed over the exact
  uploaded bytes the moment a unit finishes and *re-verified on every read*, not just stored
  decoratively: a single-job download rejects (500) if the stored copy no longer matches its
  hash, and a group combine excludes a corrupted part (noted in the summary) rather than
  silently folding bad bytes into a merged download. Exposed via `/pool/jobs` as
  `checkpoint: {unit_id, status, sha256}` and shown next to each part in the UI. The "only
  units without a saved checkpoint return to the queue" behavior was already correct before
  this (completed tasks were never re-requeued); this adds the literal checkpoint artifact the
  proposal describes.
- **Load testing + Jain's fairness index, actually run** — a new `backend/tests/load/` harness
  (Locust + a simulated-node stub, never the real deployment) measuring Objective 2's success
  criterion directly: 20 simulated students competing for 3 deliberately scarce simulated
  devices over 100 seconds. Real, not assumed, result: 142 jobs submitted, 91 completed,
  **Jain's fairness index 0.9835** (target ≥ 0.90 — passes), min/max per-student completions
  4/6, zero students starved, no over-allocation detected. Honest caveat carried over from
  `test_r3_ledger.py`: SQLite has no true row-level locking, so this proves scheduling fairness
  under genuine concurrent load, not the same rigorous concurrent-write-safety guarantee a real
  Postgres instance would.

All of the Phase 2 work was verified with new automated tests at every step (never just
asserted) and the live site was driven directly through the browser pane partway through
(logged in by the user; a live test device was spun up, exercised, and later removed) before
the focus shifted to closing code-level gaps.

## Testing

- `backend/tests/` (pytest): **72 tests** total — the original 38 (quotas, scheduler/priority,
  ledger, metrics history, parameter sweep, k-fold, consolidation, delete-cascade) plus 34 new
  in Phase 2: the Unified Quota Ledger and its CHECK constraint, job-mode fault recovery and
  the stale-sweep fix, free-memory job sizing, Random Forest, batch compile-and-test, and
  per-unit checkpoint records.
- `backend/tests/load/`: a separate Locust-based harness (not part of the pytest suite) that
  measures fairness under contention against a throwaway local server — see its own README for
  how to reproduce the 0.9835 result above.
- SQLite doesn't enforce foreign keys or CHECK constraints the same way Postgres does by
  default — the FK enforcement listener from Phase 1 covers foreign keys; the Phase 2 ledger
  CHECK constraint is declared directly on the model so SQLite enforces it too, confirmed by a
  test that deliberately violates it and expects the resulting `IntegrityError`.
- All 72 backend tests pass, no regressions. Frontend production build verified after every
  change.

## Known gaps / not done in this session

Per the proposal's own component breakdown, these are explicitly **other team members'
scope** and were intentionally left alone:
- Lease-based fault tolerance / checkpoint-resume for *sessions and PC-level self-healing*
  beyond the job-level recovery fixed in Phase 2 (R1 — Areej)
- Explainable SHAP-based intrusion detection on request-level features (R2 — Laiba)
- Secure remote-access gateway (Apache Guacamole) for off-campus sessions (R4 — Maryam)
- Pool consolidation is now explicitly listed under R4 in the updated proposal's team-area
  table, not R3 — it was built here anyway (explicitly approved earlier in the session) and
  remains in place; worth a conversation with the team about how it's credited

Also still open:
- `ids/ids/model_loader.py` downloads and unpickles a model from an unauthenticated public
  Google Drive link at startup — a real (if low-likelihood) RCE vector, flagged but not fixed.
- No CI pipeline (nothing runs tests/build automatically on push).
- The platform still runs on Render/Vercel/Supabase, not the updated proposal's target
  self-hosted campus server (Ubuntu, Docker microservices behind Nginx, PostgreSQL, MinIO,
  Guacamole) — a full infrastructure migration, not attempted here; this is a whole-team,
  deployment-target decision.
- A persisted *per-student* quota ledger table (granted/held columns) wasn't built — the
  `User`-row lock added in Phase 2 closes the actual race without it; revisit if the proposal's
  literal per-student ledger row becomes something that needs to exist, not just behave
  correctly.
- The scheduler's queue is priority + FIFO, not an explicit per-student fair-queuing mechanism
  — the measured 0.9835 Jain's index shows this converges to fairness in practice over a
  sustained run, but there's no structural guarantee for a very short burst of contention.
