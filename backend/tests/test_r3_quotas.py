"""app.quotas - one source of truth for session/job quotas, replacing two ad-hoc guards
that used to drift apart (see app/quotas.py's module docstring).

NOTE: _lock_user()'s with_for_update() is exercised here only for single-request behavior.
SQLite has no row-level locking, so the race it closes (two submissions from the same student
arriving together, each COUNT()-ing before either commits) is NOT proven by anything in this
file - that needs a real Postgres instance, same limitation as test_r3_ledger.py.
"""
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


# ---------------------------------------------------------------- StudentQuota ledger
# The proposal's literal per-student quota ledger row (app.models.StudentQuota) - a read-only
# mirror, not itself consulted by the quota decision above. See its docstring and
# app.quotas.refresh_student_quota.

def test_student_quota_ledger_reflects_an_allowed_request(client, make_user, auth):
    from app.models import StudentQuota

    student = make_user("sq1", "sq1@uog.edu.pk")
    r = client.post("/tasks/request", headers=auth(student),
                     json={"task_type": "dev_cpp", "mode": "physical", "duration_minutes": 30})
    assert r.status_code == 200

    row = StudentQuota.query.get(student.id)
    assert row is not None
    assert row.active_sessions == 1
    assert row.active_jobs == 0
    assert row.max_sessions > 0 and row.max_jobs > 0


def test_student_quota_ledger_is_still_persisted_when_a_request_is_blocked(client, make_user, auth, db):
    """The ledger write must survive even on the rejected path, where the route returns
    immediately without ever calling db.session.commit() itself - refresh_student_quota commits
    it directly in that case (see quotas.session_quota_message)."""
    from app.models import StudentQuota, TaskRequest
    from datetime import datetime, timezone

    student = make_user("sq2", "sq2@uog.edu.pk")
    db.session.add(TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
                               status="allocated", created_at=datetime.now(timezone.utc)))
    db.session.commit()

    r = client.post("/tasks/request", headers=auth(student),
                     json={"task_type": "ubuntu", "mode": "remote", "duration_minutes": 30})
    assert r.status_code == 400

    db.session.expire_all()
    row = StudentQuota.query.get(student.id)
    assert row is not None
    assert row.active_sessions == 1   # the pre-existing session, not the blocked one


def test_student_quota_ledger_tracks_jobs_independently_of_sessions(client, make_user, auth):
    from app.models import StudentQuota

    student = make_user("sq3", "sq3@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
    }, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()

    row = StudentQuota.query.get(student.id)
    assert row.active_jobs == 1
    assert row.active_sessions == 0
