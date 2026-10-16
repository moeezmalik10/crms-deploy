"""Lease-style recovery for divisible pool-job work (proposal: "when a PC fails, another PC
picks up its unit and only that small piece is repeated"). Covers the bug fixed this session:
a job already RUNNING on a node that drops used to be marked permanently failed, like a
session - it's now requeued like any other job-mode task, since the whole unit safely reruns
from scratch wherever it lands next. Physical/remote sessions and ML federated rounds are
still failed outright - they're stateful and can't be silently resumed elsewhere.
"""
from datetime import datetime, timedelta, timezone

from app.allocation_engine import release_node_tasks
from app.models import TaskRequest


def _task(student_id, node, mode, status, task_type="job_python", **extra):
    extra.setdefault("created_at", datetime.now(timezone.utc))
    return TaskRequest(user_id=student_id, task_type=task_type, mode=mode, status=status,
                       assigned_node_id=node.id, assigned_pc=node.name,
                       required_cpu=1, required_ram_mb=1024, **extra)


def test_a_running_job_requeues_instead_of_failing(app, db, make_user, make_node):
    node = make_node("FLAKY-NODE", device_key_hash="k1")
    student = make_user("r1", "r1@uog.edu.pk")
    job = _task(student.id, node, "job", "running", exit_code=None, output_tail=None,
               allocated_at=datetime.now(timezone.utc) - timedelta(minutes=2),
               start_time=datetime.now(timezone.utc) - timedelta(minutes=1))
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    with app.app_context():
        requeued, failed = release_node_tasks(node, "node heartbeat timeout")
        db.session.commit()   # release_node_tasks doesn't commit itself - same as every real caller

    assert (requeued, failed) == (1, 0)
    db.session.expire_all()
    t = TaskRequest.query.get(job_id)
    assert t.status == "pending"
    assert t.assigned_node_id is None
    assert t.allocated_at is None
    assert t.start_time is None   # stale per-run fields from the interrupted attempt are cleared


def test_a_running_physical_session_still_fails(app, db, make_user, make_node):
    """Unlike a job, a physical/remote session is stateful - it cannot be silently resumed on
    another device, so it must still fail when its node disappears."""
    node = make_node("FLAKY-NODE-2", device_key_hash="k2")
    student = make_user("r2", "r2@uog.edu.pk")
    session = _task(student.id, node, "physical", "running", task_type="dev_cpp")
    db.session.add(session)
    db.session.commit()
    session_id = session.id

    with app.app_context():
        requeued, failed = release_node_tasks(node, "node heartbeat timeout")
        db.session.commit()

    assert (requeued, failed) == (0, 1)
    db.session.expire_all()
    assert TaskRequest.query.get(session_id).status == "failed"


def test_an_allocated_but_not_yet_started_job_still_requeues(app, db, make_user, make_node):
    """Unchanged behavior from before this session: a job that never even got an agent to
    start it requeues too, same as it always did."""
    node = make_node("FLAKY-NODE-3", device_key_hash="k3")
    student = make_user("r3", "r3@uog.edu.pk")
    job = _task(student.id, node, "job", "allocated")
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    with app.app_context():
        requeued, failed = release_node_tasks(node, "node left the pool")
        db.session.commit()

    assert (requeued, failed) == (1, 0)
    db.session.expire_all()
    assert TaskRequest.query.get(job_id).status == "pending"


def test_a_stale_node_cannot_post_a_result_after_its_job_was_reassigned(app, db, make_user,
                                                                         make_node, client, auth):
    """The false-positive case: FLAKY-NODE wasn't really dead, just slow to heartbeat. Its job
    gets requeued (and, say, picked up by a second device) before FLAKY-NODE's own stale run
    finishes and tries to report a result. That stale POST must be rejected, not accepted as a
    duplicate/overwriting result - assigned_node_id has already moved on by the time it arrives."""
    from app.pool_security import sha256_hex

    stale_node = make_node("FLAKY-NODE-4", device_key_hash=sha256_hex("devkey4"))
    other_node = make_node("OTHER-NODE", device_key_hash=sha256_hex("devkey5"))
    student = make_user("r4", "r4@uog.edu.pk")
    job = _task(student.id, stale_node, "job", "running")
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    with app.app_context():
        release_node_tasks(stale_node, "node heartbeat timeout")
        db.session.commit()

    # Simulate the second device picking the requeued job up.
    db.session.expire_all()
    job = TaskRequest.query.get(job_id)
    job.assigned_node_id = other_node.id
    job.assigned_pc = other_node.name
    job.status = "running"
    db.session.commit()

    # FLAKY-NODE's own stale run finishes late and tries to report its (now meaningless) result.
    r = client.post(f"/agent/jobs/{job_id}/result", headers={"X-Device-Key": "devkey4"},
                    data={"exit_code": "0", "output": "stale output"})
    assert r.status_code == 403

    db.session.expire_all()
    assert TaskRequest.query.get(job_id).assigned_node_id == other_node.id   # untouched by the stale POST


def test_old_job_freshly_allocated_is_not_bounced_back_to_pending(app, db, make_user, make_node):
    """The ping-pong bug this session's allocated_at column fixes: the stale-"starting" sweep
    used to key off created_at, which never moves across requeues - so any job whose original
    submission was more than 5 minutes ago got swept straight back to pending the instant it
    next reached "starting", even though THIS attempt had only just begun."""
    from app.tasks import cleanup_system

    node = make_node("SWEEP-NODE", device_key_hash="k6")
    student = make_user("r5", "r5@uog.edu.pk")
    old_submission = datetime.now(timezone.utc) - timedelta(hours=2)
    job = _task(student.id, node, "job", "starting",
               created_at=old_submission,                                  # submitted long ago
               allocated_at=datetime.now(timezone.utc) - timedelta(seconds=10))  # but just (re-)allocated
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    with app.app_context():
        cleanup_system()

    db.session.expire_all()
    assert TaskRequest.query.get(job_id).status == "starting"   # left alone - this attempt is fresh


def test_a_job_truly_stuck_in_starting_is_swept_after_five_minutes(app, db, make_user, make_node):
    from app.tasks import cleanup_system

    node = make_node("SWEEP-NODE-2", device_key_hash="k7")
    student = make_user("r6", "r6@uog.edu.pk")
    job = _task(student.id, node, "job", "starting",
               allocated_at=datetime.now(timezone.utc) - timedelta(minutes=6))
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    with app.app_context():
        cleanup_system()

    db.session.expire_all()
    t = TaskRequest.query.get(job_id)
    assert t.status == "pending"
    assert t.allocated_at is None


def test_an_allocated_task_the_agent_never_polled_is_also_swept(app, db, make_user, make_node):
    """Not just "starting" (agent told, hasn't acked) - "allocated" (never even polled at all)
    used to have no timeout at all."""
    from app.tasks import cleanup_system

    node = make_node("SWEEP-NODE-3", device_key_hash="k8")
    student = make_user("r7", "r7@uog.edu.pk")
    job = _task(student.id, node, "job", "allocated",
               allocated_at=datetime.now(timezone.utc) - timedelta(minutes=6))
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    with app.app_context():
        cleanup_system()

    db.session.expire_all()
    assert TaskRequest.query.get(job_id).status == "pending"
