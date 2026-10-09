"""app.quotas - one source of truth for session/job quotas, replacing two ad-hoc guards
that used to drift apart (see app/quotas.py's module docstring)."""
import io


def test_session_quota_blocks_a_second_active_session(client, make_user, auth, db):
    from app.models import TaskRequest
    from datetime import datetime, timezone

    student = make_user("s1", "s1@uog.edu.pk")
    db.session.add(TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
                               status="allocated", created_at=datetime.now(timezone.utc)))
    db.session.commit()

    # This is exactly the bug that was fixed: "allocated" used to be missing from the status
    # list the guard checked, so a student with an allocated-but-not-started session could
    # sneak a second one through.
    r = client.post("/tasks/request", headers=auth(student),
                     json={"task_type": "ubuntu", "mode": "remote", "duration_minutes": 30})
    assert r.status_code == 400
    assert "active session" in r.get_json()["error"]


def test_session_quota_allows_a_fresh_student(client, make_user, auth):
    student = make_user("s2", "s2@uog.edu.pk")
    r = client.post("/tasks/request", headers=auth(student),
                     json={"task_type": "dev_cpp", "mode": "physical", "duration_minutes": 30})
    assert r.status_code == 200


def test_job_mode_does_not_count_against_the_session_quota(client, make_user, auth, db):
    """A pool job and an interactive session are independent quotas (app/quotas.py keeps the
    distinction the original two-guard design had, just correctly this time)."""
    from app.models import TaskRequest
    from datetime import datetime, timezone

    student = make_user("s3", "s3@uog.edu.pk")
    db.session.add(TaskRequest(user_id=student.id, task_type="job_python", mode="job",
                               status="running", created_at=datetime.now(timezone.utc)))
    db.session.commit()

    r = client.post("/tasks/request", headers=auth(student),
                     json={"task_type": "dev_cpp", "mode": "physical", "duration_minutes": 30})
    assert r.status_code == 200


def test_job_quota_blocks_a_fourth_job(client, make_user, auth):
    student = make_user("s4", "s4@uog.edu.pk")
    for _ in range(3):
        r = client.post("/pool/jobs", headers=auth(student), data={
            "file": (io.BytesIO(b"print(1)"), "main.py"),
            "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        }, content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()

    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "3 jobs" in r.get_json()["error"]
