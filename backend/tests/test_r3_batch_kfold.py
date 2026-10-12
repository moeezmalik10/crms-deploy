"""The second defined batch job type: k-fold cross-validation. Unlike parameter sweep, every
part gets the WHOLE dataset (not a row-sliced chunk) - each part is expected to do its own
internal fold split (e.g. sklearn's KFold using CRMS_PART/CRMS_PARTS) and report that one
fold's score. The group result is the average across folds, not the best one.
"""
import io
import json
import zipfile


def _upload_kfold_job(client, auth_headers, parts="5", **form):
    return client.post("/pool/jobs", headers=auth_headers, data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "data": (io.BytesIO(b"a,b,target\n1,2,0\n3,4,1\n"), "data.csv"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "kfold", "parts": parts,
        **form,
    }, content_type="multipart/form-data")


def test_kfold_needs_a_data_file(client, make_user, auth):
    student = make_user("kf1", "kf1@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "kfold", "parts": "5",
    }, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "data file" in r.get_json()["error"]


def test_kfold_clamps_an_out_of_range_fold_count(client, make_user, auth):
    from app.models import TaskRequest

    student = make_user("kf2", "kf2@uog.edu.pk")
    r = _upload_kfold_job(client, auth(student), parts="1")   # below the minimum of 2
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.chunk_id == 2, "parts below the minimum should be clamped up to 2, not rejected silently"


def test_kfold_gives_every_part_the_whole_dataset(client, make_user, auth, db):
    """The key difference from a normal split job or a sweep: no row-sharding."""
    from app.models import PoolBlob, TaskRequest

    student = make_user("kf3", "kf3@uog.edu.pk")
    r = _upload_kfold_job(client, auth(student), parts="3")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.job_kind == "kfold"
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)
    assert len(kids) == 3

    full_csv = b"a,b,target\n1,2,0\n3,4,1\n"
    for c in kids:
        blob = PoolBlob.query.get(c.input_blob_id)
        files = zipfile.ZipFile(io.BytesIO(blob.data))
        assert files.read("data.csv") == full_csv, "every fold must see the full dataset"
        # and every part shares the same job_args - no per-fold argument override like sweep
        assert c.job_args == group.job_args


def _fake_result_blob(score):
    from app.models import PoolBlob
    from app import db
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("result.json", json.dumps({"score": score}))
    blob = PoolBlob(kind="job_result", name="r.zip", size=buf.tell(), data=buf.getvalue())
    db.session.add(blob)
    db.session.flush()
    return blob.id


def test_kfold_group_json_averages_fold_scores_not_the_best(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_json
    from datetime import datetime, timezone

    student = make_user("kf4", "kf4@uog.edu.pk")
    r = _upload_kfold_job(client, auth(student), parts="3")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    for c, score in zip(kids, [0.6, 0.9, 0.75]):   # average = 0.75, NOT the max (0.9)
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_result_blob(score)
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["status"] == "completed"
    assert "best_part" not in j, "k-fold must not report a 'winner' the way a sweep does"
    assert round(j["average_score"], 4) == 0.75
    assert j["folds_scored"] == 3
    assert j["fold_scores"] == [0.6, 0.9, 0.75]


def test_kfold_group_result_zip_reports_the_average(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("kf5", "kf5@uog.edu.pk")
    r = _upload_kfold_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    for c, score in zip(kids, [0.4, 0.6]):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_result_blob(score)
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group_id))
    files = _read_zip(resp.get_data())
    summary = files["summary.txt"].decode()
    assert "average score 0.5" in summary
    avg = json.loads(files["average_result.json"])
    assert avg["average_score"] == 0.5
