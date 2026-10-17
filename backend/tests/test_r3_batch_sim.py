"""The fifth defined batch job type: batch simulations (e.g. SUMO, Monte Carlo). Split by runs
or random seeds - like k-fold/RF, every part gets the whole (optional) scenario file, not a
row-sliced chunk, and reads CRMS_PART/CRMS_PARTS to pick its own random seed for its own batch
of runs. Unlike every other job type, the combine step is CRMS actually aggregating statistics
(count/sum/mean/min/max/stdev) over every run pooled across all parts, not just picking a
winner, averaging a single score, merging opaque trees, or collecting reports.
"""
import io
import json
import zipfile


def _upload_sim_job(client, auth_headers, parts="3", **form):
    return client.post("/pool/jobs", headers=auth_headers, data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "sim", "parts": parts,
        **form,
    }, content_type="multipart/form-data")


def test_sim_does_not_need_a_data_file(client, make_user, auth):
    """Unlike k-fold/RF/compile-test, a simulation's scenario file is optional - plenty of
    simulations (Monte Carlo, a built-in SUMO scenario) need no external config at all."""
    student = make_user("sim1", "sim1@uog.edu.pk")
    r = _upload_sim_job(client, auth(student), parts="3")
    assert r.status_code == 200


def test_sim_clamps_an_out_of_range_group_count(client, make_user, auth):
    from app.models import TaskRequest

    student = make_user("sim2", "sim2@uog.edu.pk")
    r = _upload_sim_job(client, auth(student), parts="1")   # below the minimum of 2
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.chunk_id == 2, "parts below the minimum should be clamped up to 2, not rejected silently"


def test_sim_gives_every_part_the_whole_scenario_file_when_one_is_given(client, make_user, auth, db):
    from app.models import PoolBlob, TaskRequest

    student = make_user("sim3", "sim3@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "data": (io.BytesIO(b"network=small\nseed_base=42\n"), "scenario.txt"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "sim", "parts": "3",
    }, content_type="multipart/form-data")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.job_kind == "sim"
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)
    assert len(kids) == 3

    full_scenario = b"network=small\nseed_base=42\n"
    for c in kids:
        blob = PoolBlob.query.get(c.input_blob_id)
        files = zipfile.ZipFile(io.BytesIO(blob.data))
        assert files.read("scenario.txt") == full_scenario


def _fake_sim_result_blob(results):
    from app.models import PoolBlob
    from app import db
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("result.json", json.dumps({"results": results}))
    blob = PoolBlob(kind="job_result", name="r.zip", size=buf.tell(), data=buf.getvalue())
    db.session.add(blob)
    db.session.flush()
    return blob.id


def test_sim_group_json_aggregates_pooled_statistics(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_json

    student = make_user("sim4", "sim4@uog.edu.pk")
    r = _upload_sim_job(client, auth(student), parts="3")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    run_groups = [[10.0, 20.0], [30.0], [40.0, 50.0, 60.0]]   # pooled: 10,20,30,40,50,60
    for c, runs in zip(kids, run_groups):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_sim_result_blob(runs)
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["status"] == "completed"
    stats = j["sim_stats"]
    assert stats["count"] == 6
    assert stats["sum"] == 210.0
    assert stats["mean"] == 35.0
    assert stats["min"] == 10.0 and stats["max"] == 60.0
    assert stats["stdev"] > 0
    assert j["sim_groups_reporting"] == 3
    # not any other job type's shape
    assert "best_part" not in j and "average_score" not in j and "trees_total" not in j


def test_sim_group_json_handles_a_single_pooled_run_without_crashing(client, make_user, auth, db):
    """stdev of one value is 0, not a division error."""
    from app.models import TaskRequest
    from app.pool_routes import _group_json
    from datetime import datetime, timezone

    student = make_user("sim5", "sim5@uog.edu.pk")
    r = _upload_sim_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    kids = sorted(TaskRequest.query.get(group_id).sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    kids[0].status, kids[0].start_time, kids[0].completed_at = "completed", now, now
    kids[0].result_blob_id = _fake_sim_result_blob([5.0])
    kids[1].status, kids[1].start_time, kids[1].completed_at = "completed", now, now
    kids[1].result_blob_id = _fake_sim_result_blob([])   # completed, but reported zero runs
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["sim_stats"]["count"] == 1
    assert j["sim_stats"]["mean"] == 5.0
    assert j["sim_stats"]["stdev"] == 0.0


def test_sim_group_result_zip_pools_runs_in_part_order(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("sim6", "sim6@uog.edu.pk")
    r = _upload_sim_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    kids = sorted(TaskRequest.query.get(group_id).sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    kids[0].status, kids[0].start_time, kids[0].completed_at = "completed", now, now
    kids[0].result_blob_id = _fake_sim_result_blob([1.0, 2.0])
    kids[1].status, kids[1].start_time, kids[1].completed_at = "completed", now, now
    kids[1].result_blob_id = _fake_sim_result_blob([3.0])
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group_id))
    files = _read_zip(resp.get_data())
    summary = files["summary.txt"].decode()
    assert "3 run(s) pooled from 2 of 2 group(s)" in summary

    combined = json.loads(files["combined_stats.json"])
    assert combined["count"] == 3
    assert combined["results"] == [1.0, 2.0, 3.0]   # original part order
    assert combined["mean"] == 2.0
