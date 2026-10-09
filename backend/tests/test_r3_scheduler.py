"""Priority + admission control + the fairness-penalty fix.

NOTE on scope: allocate_task()/_storage_targets()/spawn_distributed_ml() all take a row lock
(with_for_update) on the Node rows they choose among, so two concurrent allocators can't both
believe the same free capacity is available. SQLite has no row-level locking, so that
concurrency guarantee is NOT exercised by these tests - only the single-request behavior is.
Verifying the lock actually prevents a race needs a real Postgres instance.
"""
from datetime import datetime, timezone


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
