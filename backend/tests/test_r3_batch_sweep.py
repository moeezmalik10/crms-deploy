"""The first defined batch job type: parameter sweep. Submission, validation, and the
best-score reduce step (_group_json/_group_result). Running the job for real needs a live
agent, which these tests don't have - see the recorded live demo earlier in this project for
proof the underlying split-job execution pipeline itself works end to end; what's new here
(per-part args, the job_kind field, and the score reduction) is what these tests cover.
"""
import io
import json
import zipfile


def _upload_job(client, auth_headers, **form):
    return client.post("/pool/jobs", headers=auth_headers, data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        **form,
    }, content_type="multipart/form-data")


def test_sweep_needs_at_least_two_parameter_sets(client, make_user, auth):
    student = make_user("sw1", "sw1@uog.edu.pk")
    r = _upload_job(client, auth(student), kind="sweep", param_sets="--lr 0.1")
    assert r.status_code == 400
    assert "at least 2" in r.get_json()["error"]


def test_sweep_creates_one_child_per_parameter_set_with_its_own_args(client, make_user, auth):
    from app.models import TaskRequest

    student = make_user("sw2", "sw2@uog.edu.pk")
    r = _upload_job(client, auth(student), kind="sweep",
                    param_sets="--lr 0.01\n--lr 0.1\n--lr 0.5")
    assert r.status_code == 200
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    assert group.job_kind == "sweep"
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)
    assert [c.job_args for c in kids] == ["--lr 0.01", "--lr 0.1", "--lr 0.5"]


def _fake_result_blob(score):
    from app.models import PoolBlob
    from app import db
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("result.json", json.dumps({"score": score}))
        z.writestr("_run_log.txt", "ok")
    blob = PoolBlob(kind="job_result", name="r.zip", size=buf.tell(), data=buf.getvalue())
    db.session.add(blob)
    db.session.flush()
    return blob.id


def test_sweep_group_json_reports_the_best_score_once_completed(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_json
    from datetime import datetime, timezone

    student = make_user("sw3", "sw3@uog.edu.pk")
    r = _upload_job(client, auth(student), kind="sweep", param_sets="--lr 0.01\n--lr 0.1\n--lr 0.5")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    scores = [0.62, 0.91, 0.77]   # part 2 (args "--lr 0.1") should win
    now = datetime.now(timezone.utc)
    for c, score in zip(kids, scores):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_result_blob(score)
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["status"] == "completed"
    assert j["best_part"] == 2
    assert j["best_score"] == 0.91
    assert j["best_args"] == "--lr 0.1"


def test_sweep_group_result_zip_names_the_winner(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("sw4", "sw4@uog.edu.pk")
    r = _upload_job(client, auth(student), kind="sweep", param_sets="--lr 0.01\n--lr 0.9")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    for c, score in zip(kids, [0.3, 0.95]):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_result_blob(score)
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group_id))
    files = _read_zip(resp.get_data())
    summary = files["summary.txt"].decode()
    assert "part 2" in summary and "0.95" in summary
    assert "best/result.json" in files
    assert json.loads(files["best/result.json"])["score"] == 0.95


def test_non_sweep_jobs_are_unaffected(client, make_user, auth, db):
    """A plain custom job (no kind) must not get a job_kind or sweep-only fields."""
    from app.models import TaskRequest
    from app.pool_routes import _group_json

    student = make_user("sw5", "sw5@uog.edu.pk")
    r = _upload_job(client, auth(student), parts="2")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.job_kind is None
    j = _group_json(group)
    assert "best_part" not in j
