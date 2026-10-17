"""DELETE /pool/jobs/<id> (cancellation) and the race it was silently losing to in production.

Caught live: a student's DELETE call returned {"status": "cancelled"} every time, but the job
reappeared as "pending" moments later. Root cause: allocate_task() had no guard against running
on a task that had already moved on for any reason *other* than already being assigned a node -
so if process_queue's periodic sweep had a task's id in its current batch at the moment it got
cancelled, allocate_task ran anyway, found no capacity, and unconditionally reset status back to
"pending" with no idea the row had just been cancelled out from under it. Fixed by having
allocate_task bail out whenever status != "pending" for any reason, and by having delete_job
lock every row it touches first, closing the race from both sides.
"""
from datetime import datetime, timezone

import io


def test_cancelling_a_pending_split_job_fails_every_part(client, make_user, auth, db):
    from app.models import TaskRequest

    student = make_user("cancel1", "cancel1@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "parts": "2",
    }, content_type="multipart/form-data")
    group_id = r.get_json()["job"]["id"]

    r2 = client.delete(f"/pool/jobs/{group_id}", headers=auth(student))
    assert r2.status_code == 200
    assert r2.get_json()["status"] == "cancelled"

    db.session.expire_all()
    kids = TaskRequest.query.filter_by(parent_task_id=group_id).all()
    assert len(kids) == 2
    assert all(c.status == "failed" for c in kids)


def test_cancelling_a_pending_single_job_fails_it(client, make_user, auth, db):
    from app.models import TaskRequest

    student = make_user("cancel2", "cancel2@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    task_id = r.get_json()["job"]["id"]

    r2 = client.delete(f"/pool/jobs/{task_id}", headers=auth(student))
    assert r2.status_code == 200
    assert r2.get_json()["status"] == "cancelled"

    db.session.expire_all()
    assert TaskRequest.query.get(task_id).status == "failed"


def test_allocate_task_does_not_resurrect_a_cancelled_task(app, db, make_user):
    """The exact race: a task is cancelled (status -> failed, never assigned a node), then
    something (a stale process_queue batch) calls allocate_task on it anyway. Before the fix,
    finding no online nodes unconditionally set status back to "pending"."""
    from app.allocation_engine import allocate_task
    from app.models import TaskRequest

    student = make_user("cancel3", "cancel3@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="failed",
                       message="Cancelled by the student", completed_at=datetime.now(timezone.utc),
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    with app.app_context():
        result = allocate_task(task_id)

    assert result["status"] == "failed"
    db.session.expire_all()
    assert TaskRequest.query.get(task_id).status == "failed"   # still cancelled, not resurrected


def test_allocate_task_still_recognizes_an_already_assigned_task(app, db, make_user, make_node):
    """The original check this replaces - an already-allocated/running task must still report
    "already_allocated" rather than being treated as eligible for a fresh placement."""
    from app.allocation_engine import allocate_task
    from app.models import TaskRequest

    node = make_node("CANCEL-NODE", device_key_hash="cxk1")
    student = make_user("cancel4", "cancel4@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="running",
                       assigned_node_id=node.id, assigned_pc=node.name,
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    with app.app_context():
        result = allocate_task(task.id)

    assert result == {"status": "already_allocated", "pc": node.name}


def test_deleting_an_already_terminal_job_group_removes_it(client, make_user, auth, db):
    from app.models import TaskRequest

    student = make_user("cancel5", "cancel5@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "parts": "2",
    }, content_type="multipart/form-data")
    group_id = r.get_json()["job"]["id"]
    for c in TaskRequest.query.filter_by(parent_task_id=group_id).all():
        c.status = "completed"
        c.completed_at = datetime.now(timezone.utc)
    db.session.commit()

    r2 = client.delete(f"/pool/jobs/{group_id}", headers=auth(student))
    assert r2.status_code == 200
    assert r2.get_json()["status"] == "deleted"

    db.session.expire_all()
    assert TaskRequest.query.get(group_id) is None
