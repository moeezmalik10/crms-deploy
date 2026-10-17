# Fairness / load-test harness

Measures the proposal's Objective 2 success criterion: *"No over-allocation of any PC under
load test; Jain's fairness index of 0.9 or higher under contention."*

This runs entirely against a throwaway local sqlite db and a local Flask server - **never**
the real deployment. Node execution is simulated (`fake_agents.py` reports every job
"completed" after a short random delay instead of actually running sandboxed code), so this
isolates the *scheduler's* fairness under contention from real job execution, which is already
covered separately by the sweep/k-fold/RF/compile-test tests.

## How it works

20 simulated students repeatedly submit trivial jobs against only 3 simulated devices -
deliberately scarce, so most requests queue and compete. Fairness is measured as each
student's count of jobs that reached `completed` by the end of the run:

```
J(x1..xn) = (sum(xi))^2 / (n * sum(xi^2))
```

1.0 is perfectly equal shares; 1/n is the worst case (one student got everything). Over-
allocation is checked against the Unified Quota Ledger columns (`Node.used_cores` /
`reserved_cores` / `used_ram_mb` / `reserved_ram_mb`), which are also guarded live by a DB
`CHECK` constraint - if the scheduler ever over-committed a node, either the constraint would
raise during the run, or it would show up directly in the final snapshot.

**Caveat, same as `test_r3_ledger.py`'s:** sqlite has no real row-level locking, so this does
not rigorously prove the concurrent-write safety claim the way a real Postgres instance would -
it measures scheduling fairness under real concurrent HTTP load, which SQLite's single-writer
serialization doesn't fake.

## Running it

```bash
cd backend
python -m venv venv  # if you don't already have one
venv/Scripts/pip install -r requirements.txt -r tests/load/requirements.txt

cd tests/load
python seed_db.py                 # fresh throwaway db + students.json + nodes.json

python run_server.py &            # terminal 1 - leave running
python fake_agents.py 140 &       # terminal 2 - simulated nodes, run for 140s

locust -f locustfile.py --host http://127.0.0.1:5055 \
       -u 20 -r 20 --headless --run-time 100s --csv results   # terminal 3

python compute_fairness.py        # after both finish
```

Stop `run_server.py` afterwards. Adjust `N_STUDENTS` / `N_NODES` in `common.py` to change the
contention ratio.

## Last measured result

20 students, 3 nodes, 100s run: **142 submitted, 91 completed, Jain's index 0.9835** (target
≥ 0.90 - pass), min/max per-student completions 4/6, zero students starved, no over-allocation.
