"""Priority + admission control + the fairness-penalty fix.

NOTE on scope: allocate_task()/_storage_targets()/spawn_distributed_ml() all take a row lock
(with_for_update) on the Node rows they choose among, so two concurrent allocators can't both
believe the same free capacity is available. SQLite has no row-level locking, so that
concurrency guarantee is NOT exercised by these tests - only the single-request behavior is.
Verifying the lock actually prevents a race needs a real Postgres instance.
"""
from datetime import datetime, timedelta, timezone


def test_default_priority_interactive_outranks_batch():
    from app.allocation_engine import default_priority, PRIORITY_INTERACTIVE, PRIORITY_BATCH
    assert default_priority("physical") == PRIORITY_INTERACTIVE
    assert default_priority("remote") == PRIORITY_INTERACTIVE
    assert default_priority("job") == PRIORITY_BATCH
    assert PRIORITY_INTERACTIVE < PRIORITY_BATCH   # lower runs first


def test_new_session_gets_interactive_priority(client, make_user, auth, db):
    from app.models import TaskRequest

    student = make_user("p1", "p1@uog.edu.pk")
    client.post("/tasks/request", headers=auth(student),
                json={"task_type": "dev_cpp", "mode": "physical", "duration_minutes": 30})
    task = TaskRequest.query.filter_by(user_id=student.id).first()
    assert task.priority == 10


def test_batch_job_defers_to_an_already_waiting_interactive_session(client, make_user, auth, db):
    """The bug this fixes: both submit paths used to call allocate_task() immediately, so a
    freshly submitted batch job could grab a device ahead of an interactive session that was
    already sitting in the queue waiting for one."""
    import io
    from app.models import TaskRequest

    student = make_user("p2", "p2@uog.edu.pk")
    # No node is online, so this session request stays "pending" - still waiting its turn.
    client.post("/tasks/request", headers=auth(student),
                json={"task_type": "dev_cpp", "mode": "physical", "duration_minutes": 30})
    waiting_session = TaskRequest.query.filter_by(user_id=student.id, mode="physical").first()
    assert waiting_session.status == "pending"

    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert body["allocation"]["status"] == "pending"
    assert "higher-priority" in body["allocation"]["message"]


def test_batch_job_allocates_immediately_with_no_contention(client, make_user, auth, make_node):
    """The fast path is preserved: with nothing else waiting, a job still gets scheduled
    right away instead of waiting for the next queue tick."""
    import io
    make_node("NODE-1", total_cores=8, total_ram_mb=8192, device_key_hash="x",
             sandbox_mode="light", allow_light_sandbox=True, free_storage_gb=50)

    student = make_user("p3", "p3@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["allocation"]["status"] == "allocated"


def test_split_job_siblings_dont_block_each_other(client, make_user, auth, db):
    """A split job's own children share one priority and must not count as 'a higher-or-equal
    priority request is already waiting' against each other (they're the same request)."""
    import io
    student = make_user("p4", "p4@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "parts": "3",
        "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    # No online nodes either way, but every part's reason must be "no device", never
    # "queued behind a higher-priority request" (that would mean siblings blocked each other).
    for a in body["allocation"]:
        assert a.get("message") != "Queued behind a higher-priority request."


def test_process_queue_does_not_starve_behind_unsatisfiable_tasks(app, db, make_user, make_node):
    """The bug: `limit(3)` ordered only by created_at meant 3 old, stuck requests silently
    blocked every later request from ever being attempted, however small or long-waiting."""
    from app.models import TaskRequest
    from app.tasks import process_queue

    student = make_user("p5", "p5@uog.edu.pk")
    # Three old requests that can never be satisfied (ml_node profile needs 2 cores/4096MB;
    # this node has none online at all, so none of the first three can ever be placed).
    for i in range(3):
        db.session.add(TaskRequest(user_id=student.id, task_type="ml_node", mode="remote",
                                   status="pending", priority=50,
                                   created_at=datetime.now(timezone.utc)))
    db.session.commit()

    make_node("NODE-2", total_cores=8, total_ram_mb=8192, status="online",
             last_heartbeat=datetime.now(timezone.utc), sandbox_mode="docker")
    # A 4th, satisfiable request submitted after the three stuck ones.
    satisfiable = TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
                              status="pending", priority=50, created_at=datetime.now(timezone.utc))
    db.session.add(satisfiable)
    db.session.commit()
    satisfiable_id = satisfiable.id

    with app.app_context():
        process_queue()
        # Session.get() would return the identity-mapped Python object from before the
        # commit above, with its original in-memory "pending" status, not a fresh row.
        db.session.expire_all()
        placed = TaskRequest.query.get(satisfiable_id)
    assert placed.status == "allocated", (
        "a satisfiable request was starved behind unsatisfiable older ones"
    )


def test_fair_queue_order_round_robins_within_a_priority_tier():
    """Unit test against plain fake task objects, independent of the DB: a burst of several
    same-priority tasks from one "student" must not occupy every early slot ahead of a single
    task from someone else who has an older task overall but fewer of them."""
    from collections import namedtuple
    from datetime import timedelta
    from app.allocation_engine import fair_queue_order

    Fake = namedtuple("Fake", "id user_id priority created_at")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Student A bursts 3 tasks first; student B's single task lands right after A's burst.
    a1 = Fake("a1", "A", 50, t0)
    a2 = Fake("a2", "A", 50, t0 + timedelta(seconds=1))
    a3 = Fake("a3", "A", 50, t0 + timedelta(seconds=2))
    b1 = Fake("b1", "B", 50, t0 + timedelta(seconds=3))

    order = [t.id for t in fair_queue_order([a1, a2, a3, b1])]
    assert order == ["a1", "b1", "a2", "a3"]


def test_fair_queue_order_respects_priority_tiers_first():
    """Round-robin only ever reorders within a tier - a higher-priority (lower number) task
    always still comes before every lower-priority one, burst or not."""
    from collections import namedtuple
    from datetime import timedelta
    from app.allocation_engine import fair_queue_order

    Fake = namedtuple("Fake", "id user_id priority created_at")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    batch1 = Fake("batch1", "A", 50, t0)
    batch2 = Fake("batch2", "A", 50, t0 + timedelta(seconds=1))
    interactive = Fake("interactive", "B", 10, t0 + timedelta(seconds=2))

    order = [t.id for t in fair_queue_order([batch1, batch2, interactive])]
    assert order == ["interactive", "batch1", "batch2"]


def test_fair_queue_order_is_plain_fifo_for_a_single_student():
    """No behavior change for the common case: one student's own backlog comes out in exactly
    the order it was submitted."""
    from collections import namedtuple
    from datetime import timedelta
    from app.allocation_engine import fair_queue_order

    Fake = namedtuple("Fake", "id user_id priority created_at")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [Fake(f"t{i}", "A", 50, t0 + timedelta(seconds=i)) for i in range(5)]

    order = [t.id for t in fair_queue_order(tasks)]
    assert order == ["t0", "t1", "t2", "t3", "t4"]


def test_process_queue_serves_a_second_student_in_the_same_tick_as_a_first_students_burst(
        app, db, make_user, make_node):
    """The structural gap plain FIFO left: a node with room for exactly 2 concurrent job-mode
    units, one student bursts 5 queued jobs, a second student's single job lands right after
    that burst. Under pure (priority, created_at) FIFO the first 2 candidates attempted would
    both be the bursty student's (starving the second student's request for this whole tick,
    and potentially several more if the burst is large enough); fair_queue_order's round-robin
    means the second student's job gets a turn in the very first round instead."""
    from app.models import TaskRequest
    from app.tasks import process_queue

    # Sized so exactly 2 concurrent 1-core/1024MB job units fit: buffer_cores = 5% of 4 = 0.2
    # (cap_cpu 3.8, 2 units use 2), buffer_ram = 768 fixed (cap_ram 2916-768=2148, 2 units use
    # 2048 but a 3rd would need 3072 > 2148).
    make_node("FAIR-NODE", total_cores=4, total_ram_mb=2916, status="online",
             last_heartbeat=datetime.now(timezone.utc), device_key_hash="fair-node-key",
             sandbox_mode="docker", free_storage_gb=50)

    bursty = make_user("fq_bursty", "fq_bursty@uog.edu.pk")
    other = make_user("fq_other", "fq_other@uog.edu.pk")

    t0 = datetime.now(timezone.utc)
    bursty_ids = []
    for i in range(5):
        t = TaskRequest(user_id=bursty.id, task_type="job_python", mode="job", status="pending",
                        priority=50, required_cpu=1, required_ram_mb=1024,
                        created_at=t0 - timedelta(seconds=10 - i))
        db.session.add(t)
        db.session.flush()
        bursty_ids.append(t.id)

    other_task = TaskRequest(user_id=other.id, task_type="job_python", mode="job", status="pending",
                             priority=50, required_cpu=1, required_ram_mb=1024,
                             created_at=t0 - timedelta(seconds=4))   # right after the burst
    db.session.add(other_task)
    db.session.commit()
    other_id = other_task.id

    with app.app_context():
        process_queue()
        # Session.get() outside this block would return the identity-mapped Python objects
        # from before process_queue's own nested app-context session committed, not a fresh
        # row - same caveat as test_process_queue_does_not_starve_behind_unsatisfiable_tasks
        # above. Read everything needed into plain values before the block exits.
        db.session.expire_all()
        other_status = TaskRequest.query.get(other_id).status
        allocated_bursty = sum(
            1 for tid in bursty_ids if TaskRequest.query.get(tid).status == "allocated"
        )

    assert other_status == "allocated", (
        "the second student's request was starved behind the first student's burst"
    )
    assert allocated_bursty == 1   # exactly one of the burst got the node's one remaining slot


def test_job_mode_counts_against_the_fairness_penalty(client, make_user, auth, make_node, db):
    """The bug: task_count only summed phys+rem, so a node already running several sandboxed
    jobs looked exactly as attractive to the scorer as a completely idle one."""
    import io
    from app.models import TaskRequest

    busy = make_node("BUSY", total_cores=8, total_ram_mb=8192, device_key_hash="k1",
                     sandbox_mode="light", allow_light_sandbox=True, free_storage_gb=50)
    idle = make_node("IDLE", total_cores=8, total_ram_mb=8192, device_key_hash="k2",
                     sandbox_mode="light", allow_light_sandbox=True, free_storage_gb=50)
    other_student = make_user("p6_other", "p6o@uog.edu.pk")
    student = make_user("p6", "p6@uog.edu.pk")

    # Pre-load BUSY with several running jobs (not physical/remote, so the old code saw 0) -
    # owned by a different student so this doesn't also trip p6's own job quota.
    for _ in range(5):
        db.session.add(TaskRequest(user_id=other_student.id, task_type="job_python", mode="job",
                                   status="running", assigned_node_id=busy.id, assigned_pc=busy.name,
                                   required_cpu=1, required_ram_mb=128,
                                   created_at=datetime.now(timezone.utc)))
    db.session.commit()

    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.get_json()["allocation"]["assigned_machine"] == "IDLE"
