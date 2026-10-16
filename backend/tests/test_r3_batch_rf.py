"""The third defined batch job type: Random Forest training. Like k-fold, every part gets the
WHOLE dataset (not a row-sliced chunk) - each part trains its own group of trees with its own
random seed (e.g. via CRMS_PART/CRMS_PARTS) and reports those trees. The group result merges
every part's trees into one combined forest, concatenated (not deserialized) from JSON - see
_job_trees' docstring in app.pool_routes for why this never unpickles anything.
"""
import io
import json
import zipfile


def _upload_rf_job(client, auth_headers, parts="5", **form):
    return client.post("/pool/jobs", headers=auth_headers, data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "data": (io.BytesIO(b"a,b,target\n1,2,0\n3,4,1\n"), "data.csv"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "rf", "parts": parts,
        **form,
    }, content_type="multipart/form-data")


def test_rf_needs_a_data_file(client, make_user, auth):
    student = make_user("rf1", "rf1@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "rf", "parts": "5",
    }, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "data file" in r.get_json()["error"]


def test_rf_clamps_an_out_of_range_group_count(client, make_user, auth):
    from app.models import TaskRequest

    student = make_user("rf2", "rf2@uog.edu.pk")
    r = _upload_rf_job(client, auth(student), parts="1")   # below the minimum of 2
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.chunk_id == 2, "parts below the minimum should be clamped up to 2, not rejected silently"


def test_rf_gives_every_part_the_whole_dataset(client, make_user, auth, db):
    """Same reasoning as k-fold: a tree trained on only a slice of the rows would make the
    merged forest worse, not better - every part needs the full dataset."""
    from app.models import PoolBlob, TaskRequest

    student = make_user("rf3", "rf3@uog.edu.pk")
    r = _upload_rf_job(client, auth(student), parts="3")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.job_kind == "rf"
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)
    assert len(kids) == 3

    full_csv = b"a,b,target\n1,2,0\n3,4,1\n"
    for c in kids:
        blob = PoolBlob.query.get(c.input_blob_id)
        files = zipfile.ZipFile(io.BytesIO(blob.data))
        assert files.read("data.csv") == full_csv, "every tree group must see the full dataset"
        assert c.job_args == group.job_args   # no per-part argument override, unlike sweep


def _fake_tree_result_blob(trees):
    from app.models import PoolBlob
    from app import db
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("result.json", json.dumps({"trees": trees}))
    blob = PoolBlob(kind="job_result", name="r.zip", size=buf.tell(), data=buf.getvalue())
    db.session.add(blob)
    db.session.flush()
    return blob.id


def test_rf_group_json_reports_trees_merged_not_a_winner(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_json
    from datetime import datetime, timezone

    student = make_user("rf4", "rf4@uog.edu.pk")
    r = _upload_rf_job(client, auth(student), parts="3")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    tree_groups = [
        [{"seed": 1, "tree": 0}, {"seed": 1, "tree": 1}],          # 2 trees
        [{"seed": 2, "tree": 0}, {"seed": 2, "tree": 1}, {"seed": 2, "tree": 2}],   # 3 trees
        [{"seed": 3, "tree": 0}],                                   # 1 tree
    ]
    for c, trees in zip(kids, tree_groups):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_tree_result_blob(trees)
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["status"] == "completed"
    assert "best_part" not in j and "average_score" not in j
    assert j["trees_total"] == 6
    assert j["groups_merged"] == 3
    assert j["tree_counts"] == {kids[0].chunk_id: 2, kids[1].chunk_id: 3, kids[2].chunk_id: 1}


def test_rf_group_result_zip_merges_trees_in_part_order(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("rf5", "rf5@uog.edu.pk")
    r = _upload_rf_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    tree_groups = [[{"id": "a"}, {"id": "b"}], [{"id": "c"}]]
    for c, trees in zip(kids, tree_groups):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_tree_result_blob(trees)
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group_id))
    files = _read_zip(resp.get_data())
    summary = files["summary.txt"].decode()
    assert "merged 3 trees from 2 of 2 tree group(s)" in summary

    combined = json.loads(files["combined_forest.json"])
    assert combined["n_estimators"] == 3
    assert combined["trees"] == [{"id": "a"}, {"id": "b"}, {"id": "c"}]   # original part order


def test_rf_group_result_handles_a_group_that_never_reported_trees(client, make_user, auth, db):
    """A part that fails (or writes result.json without a usable "trees" field) shouldn't
    crash the combine step - it just contributes nothing to the merged forest."""
    from app.models import TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("rf6", "rf6@uog.edu.pk")
    r = _upload_rf_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    kids[0].status, kids[0].start_time, kids[0].completed_at = "completed", now, now
    kids[0].result_blob_id = _fake_tree_result_blob([{"id": "only-one"}])
    kids[1].status, kids[1].start_time, kids[1].completed_at = "failed", now, now
    kids[1].exit_code = 1
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group_id))
    files = _read_zip(resp.get_data())
    combined = json.loads(files["combined_forest.json"])
    assert combined["n_estimators"] == 1
    assert combined["trees"] == [{"id": "only-one"}]
