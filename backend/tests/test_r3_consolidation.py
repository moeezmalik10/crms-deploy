"""Consolidation: "if no single PC has room, the system moves divisible work elsewhere to
make room" (proposal section 1.3). Only ever evicts job-mode (divisible) tasks, never another
physical/remote (non-divisible) session - that's someone else's own session and must be left
alone regardless of how badly the new request needs the room.
"""
from datetime import datetime, timezone

from app.allocation_engine import allocate_task
from app.models import TaskRequest


def _job(student_id, node, cpu, ram_mb):
    return TaskRequest(user_id=student_id, task_type="job_python", mode="job", status="running",
                       assigned_node_id=node.id, assigned_pc=node.name,
                       required_cpu=cpu, required_ram_mb=ram_mb,
                       created_at=datetime.now(timezone.utc))


def test_remote_session_evicts_jobs_to_make_room(app, db, make_user, make_node):
    """A node has no room for a workspace because jobs are using up its shared slice; moving
    one of those jobs elsewhere should free enough room for the workspace to land there."""
    node = make_node("NODE-1", total_cores=8, total_ram_mb=8192, device_key_hash="k1",
                     sandbox_mode="docker", share_cores=2, share_ram_mb=4096)
    student = make_user("c1", "c1@uog.edu.pk")
    other = make_user("c1_other", "c1o@uog.edu.pk")

    big_job = _job(other.id, node, cpu=2, ram_mb=4096)   # uses the whole shared slice
    db.session.add(big_job)
    db.session.commit()
    big_job_id = big_job.id

    task = TaskRequest(user_id=student.id, task_type="vs_code", mode="remote", status="pending",
                       priority=10, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    with app.app_context():
        result = allocate_task(task.id)

    assert result["status"] == "allocated"
    assert result["assigned_machine"] == "NODE-1"
    assert result["consolidated"] == 1

    db.session.expire_all()
    evicted = TaskRequest.query.get(big_job_id)
    assert evicted.status == "pending"
    assert evicted.assigned_node_id is None
    assert "make room" in evicted.message


def test_consolidation_evicts_the_fewest_jobs_possible(app, db, make_user, make_node):
    """Two jobs on the node; the session only needs enough room for one eviction, so exactly
    one job (the biggest, freeing the most per eviction) should move, not both."""
    node = make_node("NODE-2", total_cores=8, total_ram_mb=8192, device_key_hash="k2",
                     sandbox_mode="docker", share_cores=4, share_ram_mb=6144)
    student = make_user("c2", "c2@uog.edu.pk")
    other = make_user("c2_other", "c2o@uog.edu.pk")

    small_job = _job(other.id, node, cpu=1, ram_mb=512)
    big_job = _job(other.id, node, cpu=1, ram_mb=4096)
    db.session.add_all([small_job, big_job])
    db.session.commit()
    small_id, big_id = small_job.id, big_job.id

    # vs_code needs 2 cores / 4096 MB. Shared slice is 6144 MB; both jobs use 4608 MB, so as-is
    # only 1536 MB is free (not enough). Evicting just the big job frees enough (5632 MB left);
    # the small one doesn't need to move.
    task = TaskRequest(user_id=student.id, task_type="vs_code", mode="remote", status="pending",
                       priority=10, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    with app.app_context():
        result = allocate_task(task.id)

    assert result["status"] == "allocated"
    assert result["consolidated"] == 1

    db.session.expire_all()
    assert TaskRequest.query.get(big_id).status == "pending"
    assert TaskRequest.query.get(small_id).status == "running"   # left alone - not needed


def test_physical_session_evicts_every_job_on_the_node(app, db, make_user, make_node):
    """A physical session takes the WHOLE device, so if a node's only occupant is job-mode
    work, every job there must be moved, regardless of whether a smaller subset would free
    enough CPU/RAM alone - physical mode doesn't share a PC with anything."""
    node = make_node("NODE-3", total_cores=8, total_ram_mb=8192, device_key_hash="k3",
                     sandbox_mode="docker")
    student = make_user("c3", "c3@uog.edu.pk")
    other = make_user("c3_other", "c3o@uog.edu.pk")

    j1 = _job(other.id, node, cpu=1, ram_mb=512)
    j2 = _job(other.id, node, cpu=1, ram_mb=512)
    db.session.add_all([j1, j2])
    db.session.commit()
    j1_id, j2_id = j1.id, j2.id

    task = TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical", status="pending",
                       priority=10, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    with app.app_context():
        result = allocate_task(task.id)

    assert result["status"] == "allocated"
    assert result["consolidated"] == 2
    db.session.expire_all()
    assert TaskRequest.query.get(j1_id).status == "pending"
    assert TaskRequest.query.get(j2_id).status == "pending"


def test_physical_session_never_evicts_someone_elses_session(app, db, make_user, make_node):
    """A node running another student's remote session (plus a job) must never be touched for
    a physical request - that remote session is non-divisible work, not ours to move."""
    busy_node = make_node("NODE-4", total_cores=8, total_ram_mb=8192, device_key_hash="k4",
                          sandbox_mode="docker")
    free_node = make_node("NODE-5", total_cores=8, total_ram_mb=8192, device_key_hash="k5",
                         sandbox_mode="docker")
    student = make_user("c4", "c4@uog.edu.pk")
    other = make_user("c4_other", "c4o@uog.edu.pk")

    db.session.add(TaskRequest(user_id=other.id, task_type="vs_code", mode="remote",
                               status="running", assigned_node_id=busy_node.id,
                               assigned_pc=busy_node.name, required_cpu=1, required_ram_mb=512,
                               created_at=datetime.now(timezone.utc)))
    db.session.add(_job(other.id, busy_node, cpu=1, ram_mb=512))
    db.session.commit()

    task = TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical", status="pending",
                       priority=10, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    with app.app_context():
        result = allocate_task(task.id)

    # free_node has no load at all, so normal (non-consolidation) allocation should have
    # picked it directly - busy_node's job was never touched.
    assert result["status"] == "allocated"
    assert result["assigned_machine"] == "NODE-5"
    assert result["consolidated"] == 0


def test_no_consolidation_possible_leaves_task_pending(app, db, make_user, make_node):
    """Evicting every job on the only candidate node still isn't enough room - the task must
    stay pending, not get force-placed somewhere that still won't fit."""
    node = make_node("NODE-6", total_cores=2, total_ram_mb=2048, device_key_hash="k6",
                     sandbox_mode="docker")
    student = make_user("c5", "c5@uog.edu.pk")
    other = make_user("c5_other", "c5o@uog.edu.pk")

    db.session.add(_job(other.id, node, cpu=1, ram_mb=512))
    db.session.commit()

    # android_studio needs 4 cores / 8192 MB - this 2-core/2GB node can never fit it, with
    # or without evictions.
    task = TaskRequest(user_id=student.id, task_type="android_studio", mode="physical",
                       status="pending", priority=10, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    with app.app_context():
        result = allocate_task(task.id)

    assert result["status"] == "pending"
    db.session.expire_all()
    assert TaskRequest.query.get(task_id).status == "pending"
