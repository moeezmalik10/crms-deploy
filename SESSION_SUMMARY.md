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
- **Batch compile-and-test** (`kind=compile_test`) — fourth defined batch job type. Unlike
  k-fold/RF, test cases are *row-sliced* across parts (same mechanism as the plain split job) —
  the point is running a fixed test suite faster, not training on the whole thing in every part.
  Each part writes `passed`/`failed` counts and a `tests` array; combine step sums counts and
  concatenates the array.
- **Per-unit checkpoint records** — `TaskRequest.result_sha256`, computed over the exact
  uploaded bytes the moment a unit finishes and *re-verified on every read*, not just stored
  decoratively: a single-job download rejects (500) if the stored copy no longer matches its
  hash, and a group combine excludes a corrupted part (noted in the summary) rather than
  silently folding bad bytes into a merged download. Exposed via `/pool/jobs` as
  `checkpoint: {unit_id, status, sha256}` and shown next to each part in the UI. The "only
  units without a saved checkpoint return to the queue" behavior was already correct before
  this (completed tasks were never re-requeued); this adds the literal checkpoint artifact the
  proposal describes.
- **Batch simulations** (`kind=sim`) — fifth defined batch job type, and the one genuinely
  missing after the compile-and-test work: an earlier summary in this session incorrectly
  claimed "all six job types are now covered" when only five of six (four explicit kinds plus
  the generic split path standing in for "chunked data processing") actually existed. Like
  k-fold/RF, every group gets the whole (optional) scenario file and reads
  `CRMS_PART`/`CRMS_PARTS` to pick its own random seed for its own batch of runs. Unlike every
  other job type, the combine step is CRMS itself *aggregating statistics* — count, sum, mean,
  min, max, standard deviation pooled across every group's runs — matching the proposal's own
  wording for this row rather than reusing another type's reduce. The data file is optional
  here (unlike k-fold/RF/compile-test), since plenty of simulations need no external config at
  all. All six job types from the proposal's table are now genuinely covered.
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

## 5. Live production verification

After each Phase 2 deploy, re-checked directly against the live backend rather than trusting
the deploy alone:
- Confirmed the Postgres migration (new ledger columns, `allocated_at`, `result_sha256`, both
  `CHECK` constraints) applied cleanly with no 500s anywhere.
- Submitted real jobs of every new kind (`rf`, `compile_test`, `sim` with and without a data
  file) straight against the live API and confirmed each created the right shape of job group,
  then cancelled them all afterward - nothing left dangling in production.
- `/pool/ledger` confirmed serving the refactored shared `compute_node_usage()` correctly.
- Removed the **MOEEZ-2** test device (registered earlier in the session for live testing) from
  the pool once it was no longer needed.
- Fixed a stale value in `node-agent/backend_url.txt` (pointed at a backend address that
  doesn't exist - Render had appended `-cusl` to the real one since the short name was taken,
  exactly as `DEPLOY_GUIDE.md` itself describes happening) so it matches the address already
  checked into `tools/deploy_config.ps1`.

## 6. Two real bugs found by actually running things, not just asserting against fixtures

Both caught *after* the Phase 2 feature work looked complete and fully tested — neither was
something the pytest fixtures could have caught, and both are now fixed, tested, and confirmed
live.

- **Checkpoint-hash verification gap in every kind-specific reducer.** A new
  `backend/tests/real_exec/` harness actually trains real `scikit-learn` Random Forest trees
  through a real polling agent (no stub) against a real running server, specifically to verify
  the tree-merge and checkpoint-hash logic against genuine output instead of hand-built
  fixtures. First run surfaced a real bug: `_job_scores`/`_job_trees`/`_job_reports`/
  `_job_sim_results` (sweep/k-fold/RF/compile-test/sim's combine helpers) each independently
  re-fetched a part's stored blob with **no checkpoint-hash check at all** - the generic
  per-part file loop in `_group_result` correctly excluded a corrupted part from the raw file
  listing, but every one of these specialized reducers still happily merged its score/trees/
  report/results anyway. Every pytest fixture test missed this because none of them exercised
  more than one reducer's corruption path at once. Fixed by routing all four through one shared
  `_verified_results()` helper. Confirmed fixed against real data twice: before the fix,
  corrupting one part of a real-trained RF job left the merged forest at 6 trees (wrong,
  included the corrupted part); after, it correctly drops to 3.
- **A race that silently undid job cancellation in production.** Found live, independently of
  the above: `DELETE /pool/jobs/<id>` consistently reported `{"status": "cancelled"}`, but the
  job reappeared as `"pending"` moments later - did not reproduce locally, since it needs real
  contention with `process_queue` against a real backlog of queued jobs with zero online
  devices, which production has and the local suite never did. Root cause: `allocate_task()`
  had no guard against running on a task that had already moved on for any reason *other* than
  being assigned a node - `process_queue` sweeps a batch of "pending" tasks every 15s, and if a
  student cancels a task already in that batch, `allocate_task` runs on it anyway, finds no
  capacity, and unconditionally resets status back to `"pending"`, undoing the cancellation
  with no idea it happened. Fixed from both sides: `allocate_task()` now bails out whenever
  status isn't `"pending"` for any reason, and `delete_job()` now locks every row it touches
  before mutating (same deterministic lock-ordering discipline as `lock_online_nodes()`).
  Confirmed fixed live: cancelled three real stuck production jobs, re-checked after 5s and
  again after 20s (spanning a full `process_queue` cycle) - all held as `"failed"`, no
  resurrection.

## 7. IDS model_loader.py RCE vector — closed

The one item left on the "known gaps" list from §4 that was actually fixable without another
team member's component: `ids/ids/model_loader.py` downloaded a pickle from a public Google
Drive link at startup and deserialized it with plain `pickle.load()` - a crafted `__reduce__`
in that file could run arbitrary code (classically `os.system`/`subprocess.Popen`) the instant
it's deserialized, with nothing in between to object. Two independent gates now stand between
that download and the running service:

- **Pinned SHA-256 hash**, computed from the exact file already in production, checked on
  every load (not just right after a fresh download - a cached file could be swapped or
  corrupted since).
- **A restricted unpickler** whose `find_class()` only allows the 8 classes the real
  `RandomForestClassifier` actually needs (its own structure plus numpy's array-reconstruction
  helpers) - discovered by statically walking the pickle's own opcode stream before ever
  trusting it, not by guessing. Anything else is refused outright.

Verified, not assumed: the real production model file still loads correctly (same
`RandomForestClassifier`, 78 features); a deliberately-corrupted copy is rejected by the hash
check and fails safe to the existing rule-based-only mode; a genuine crafted
`os.system(...)` payload was blocked before execution - the file it would have written never
appeared. 8 new tests in a new `ids/tests/` (this service had none before).

## Testing

- `backend/tests/` (pytest): **84 tests** total — the original 38 (quotas, scheduler/priority,
  ledger, metrics history, parameter sweep, k-fold, consolidation, delete-cascade) plus 46 new
  in Phase 2: the Unified Quota Ledger and its CHECK constraint, job-mode fault recovery and
  the stale-sweep fix, free-memory job sizing, Random Forest, batch compile-and-test, batch
  simulations, per-unit checkpoint records, the cross-cutting checkpoint-verification fix, and
  the job-cancellation race fix.
- `backend/tests/load/`: a separate Locust-based harness (not part of the pytest suite) that
  measures fairness under contention against a throwaway local server — see its own README for
  how to reproduce the 0.9835 result above.
- `backend/tests/real_exec/`: a separate harness (not part of the pytest suite) that actually
  trains real Random Forest trees through a real polling agent against a real running server -
  see its own README; this is what caught the checkpoint-verification gap above.
- `ids/tests/` (pytest): **8 tests**, new - this service had no test coverage before §7. Covers
  the pinned-hash check and the restricted unpickler, including a real `os.system(...)` and a
  real `eval(...)` payload that must both be blocked.
- SQLite doesn't enforce foreign keys or CHECK constraints the same way Postgres does by
  default — the FK enforcement listener from Phase 1 covers foreign keys; the Phase 2 ledger
  CHECK constraint is declared directly on the model so SQLite enforces it too, confirmed by a
  test that deliberately violates it and expects the resulting `IntegrityError`.
- All 84 backend tests pass, no regressions. Frontend production build verified after every
  change, and every job-type addition and bug fix re-verified directly against the live backend
  after deploy (see §5).

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
