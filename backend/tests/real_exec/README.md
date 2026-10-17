# Real-execution verification: Random Forest tree-merging + checkpoint hashing

The pytest suite (`backend/tests/test_r3_batch_rf.py`, `test_r3_checkpoint.py`) verifies the
tree-merge and checkpoint-hash logic against hand-built fixtures - fast, but it never proves
the *real* path works: a real agent downloading real job input, actually running a real
`scikit-learn` `RandomForestClassifier`, and the backend correctly merging and hash-verifying
genuine output. This harness does that, end to end, against a throwaway local server (never
the real deployment).

**What it caught, the one time it mattered:** running this the first time surfaced a real bug
that every pytest fixture test missed - `_job_scores`/`_job_trees`/`_job_reports`/
`_job_sim_results` each independently re-fetched a part's stored blob with no checkpoint-hash
check at all, so a corrupted part was correctly excluded from the generic per-part file listing
but still silently merged into the sweep/k-fold/RF/compile-test/sim result. Fixed by routing
all four through one `_verified_results()` helper; see `backend/app/pool_routes.py` and the new
regression test in `test_r3_checkpoint.py`.

## How it works

1. `seed_db.py` - one student, one online/job-capable device, in a fresh throwaway sqlite db.
2. `run_server.py` - the real Flask app against that db.
3. `real_agent.py` - polls for assigned jobs the way the real node-agent does, but actually
   downloads the job input and runs it as a real subprocess (no Docker/Windows-sandbox
   isolation needed for this - the point is exercising the backend's own logic against genuine
   output, not re-testing sandboxing, which is a different team member's area).
4. `rf_train.py` - the real program the "student" submits: trains a real
   `RandomForestClassifier` per part (reading `CRMS_PART`/`CRMS_PARTS` the same way a real
   submission would) and writes a JSON (never pickled) tree representation.
5. `verify.py` - submits a real `kind=rf` job, waits for the real agent to finish it, checks
   the per-part checkpoint hashes and the merged forest's genuine sklearn tree structure, then
   deliberately corrupts one stored part's bytes and confirms a second download correctly
   excludes it and reports a smaller merged forest - not just from the file listing, but from
   the actual merged tree count.

## Running it

```bash
cd backend
venv/Scripts/pip install -r tests/real_exec/requirements.txt

cd tests/real_exec
python seed_db.py
python run_server.py &       # leave running
python real_agent.py 70 &    # real agent, polls for 70s
python verify.py             # submits, waits, checks, corrupts, re-checks, cleans up
```

Stop `run_server.py`/`real_agent.py` afterward. `real_exec.db` and `work/` are gitignored.
