"""The checkpoint record Component 5 describes: "unit ID, status and a SHA-256 hash of the
output", taken the moment a unit finishes and re-checked whenever that result is served back
out, so silent corruption of the stored copy is caught rather than handed to a student
unnoticed. Unit ID and status are the row's own id/status; TaskRequest.result_sha256 is the
hash, computed in app.pool_agent_routes.job_result() and checked again in
app.pool_routes.job_result_download()/_group_result().
"""
import hashlib
import io
import json
import zipfile

from app.pool_security import sha256_hex


def _make_result_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("output/result.json", '{"score": 1}')
    return buf.getvalue()


def test_job_result_stores_a_sha256_checkpoint_of_the_uploaded_bytes(client, make_user, make_node, db):
    from app.models import TaskRequest
    from datetime import datetime, timezone

    node = make_node("CK-NODE", device_key_hash=sha256_hex("ckkey1"))
    student = make_user("ck1", "ck1@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="running",
                       assigned_node_id=node.id, assigned_pc=node.name,
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    payload = _make_result_zip()
    r = client.post(f"/agent/jobs/{task_id}/result", headers={"X-Device-Key": "ckkey1"},
                    data={"exit_code": "0", "output": "ok",
                          "result": (io.BytesIO(payload), "result.zip")})
    assert r.status_code == 200

    db.session.expire_all()
    t = TaskRequest.query.get(task_id)
    assert t.status == "completed"
    assert t.result_sha256 == hashlib.sha256(payload).hexdigest()


def test_job_result_without_a_file_has_no_checkpoint(client, make_user, make_node, db):
    """exit_code-only completion (no result file) has nothing to checkpoint - must not crash
    or record a hash of nothing."""
    from app.models import TaskRequest
    from datetime import datetime, timezone

    node = make_node("CK-NODE-2", device_key_hash=sha256_hex("ckkey2"))
    student = make_user("ck2", "ck2@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="running",
                       assigned_node_id=node.id, assigned_pc=node.name,
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    r = client.post(f"/agent/jobs/{task_id}/result", headers={"X-Device-Key": "ckkey2"},
                    data={"exit_code": "0", "output": "ok"})
    assert r.status_code == 200
    db.session.expire_all()
    assert TaskRequest.query.get(task_id).result_sha256 is None


def test_download_serves_a_result_whose_hash_still_matches(client, make_user, auth, db):
    from app.models import PoolBlob, TaskRequest
    from datetime import datetime, timezone

    student = make_user("ck3", "ck3@uog.edu.pk")
    payload = _make_result_zip()
    blob = PoolBlob(owner_user_id=student.id, kind="job_result", name="r.zip",
                    size=len(payload), data=payload)
    db.session.add(blob)
    db.session.flush()
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="completed",
                       result_blob_id=blob.id, result_sha256=hashlib.sha256(payload).hexdigest(),
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    r = client.get(f"/pool/jobs/{task.id}/result", headers=auth(student))
    assert r.status_code == 200
    assert r.data == payload


def test_download_rejects_a_result_whose_hash_no_longer_matches(client, make_user, auth, db):
    """Simulates the stored copy being corrupted after the checkpoint hash was recorded - the
    download must fail loudly (500) instead of silently handing back bad bytes."""
    from app.models import PoolBlob, TaskRequest
    from datetime import datetime, timezone

    student = make_user("ck4", "ck4@uog.edu.pk")
    payload = _make_result_zip()
    blob = PoolBlob(owner_user_id=student.id, kind="job_result", name="r.zip",
                    size=len(payload), data=payload)
    db.session.add(blob)
    db.session.flush()
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="completed",
                       result_blob_id=blob.id, result_sha256="0" * 64,   # wrong hash on purpose
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    r = client.get(f"/pool/jobs/{task.id}/result", headers=auth(student))
    assert r.status_code == 500
    assert "checkpoint hash" in r.get_json()["error"]


def test_group_result_skips_a_corrupted_part_but_still_combines_the_rest(client, make_user, auth, db):
    from app.models import PoolBlob, TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("ck5", "ck5@uog.edu.pk")
    now = datetime.now(timezone.utc)
    group = TaskRequest(user_id=student.id, task_type="job_python", mode="job_group",
                        status="group", chunk_id=2, message="test group", job_kind=None,
                        required_cpu=1, required_ram_mb=256, created_at=now)
    db.session.add(group)
    db.session.flush()

    good_payload = _make_result_zip()
    good_blob = PoolBlob(owner_user_id=student.id, kind="job_result", name="good.zip",
                         size=len(good_payload), data=good_payload)
    db.session.add(good_blob)
    db.session.flush()
    good_part = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="completed",
                            parent_task_id=group.id, chunk_id=1, result_blob_id=good_blob.id,
                            result_sha256=hashlib.sha256(good_payload).hexdigest(),
                            start_time=now, completed_at=now, exit_code=0,
                            required_cpu=1, required_ram_mb=256, created_at=now)

    bad_payload = _make_result_zip()
    bad_blob = PoolBlob(owner_user_id=student.id, kind="job_result", name="bad.zip",
                        size=len(bad_payload), data=bad_payload)
    db.session.add(bad_blob)
    db.session.flush()
    bad_part = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="completed",
                           parent_task_id=group.id, chunk_id=2, result_blob_id=bad_blob.id,
                           result_sha256="0" * 64,   # wrong hash on purpose
                           start_time=now, completed_at=now, exit_code=0,
                           required_cpu=1, required_ram_mb=256, created_at=now)
    db.session.add_all([good_part, bad_part])
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group.id))
    files = _read_zip(resp.get_data())
    assert "part_1/output/result.json" in files   # good part made it in
    assert "part_2/output/result.json" not in files   # bad part was excluded
    assert "result check failed" in files["summary.txt"].decode()


def test_job_json_exposes_the_checkpoint_record(client, make_user, make_node, db, auth):
    from app.models import TaskRequest
    from datetime import datetime, timezone

    node = make_node("CK-NODE-6", device_key_hash=sha256_hex("ckkey6"))
    student = make_user("ck6", "ck6@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="running",
                       assigned_node_id=node.id, assigned_pc=node.name,
                       required_cpu=1, required_ram_mb=256, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    payload = _make_result_zip()
    client.post(f"/agent/jobs/{task_id}/result", headers={"X-Device-Key": "ckkey6"},
               data={"exit_code": "0", "output": "ok", "result": (io.BytesIO(payload), "result.zip")})

    r = client.get("/pool/jobs", headers=auth(student))
    job = next(j for j in r.get_json() if j["id"] == task_id)
    assert job["checkpoint"]["unit_id"] == task_id
    assert job["checkpoint"]["status"] == "completed"
    assert job["checkpoint"]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_a_corrupted_part_is_excluded_from_every_kind_specific_reducer(client, make_user, db):
    """Caught live by an end-to-end real-execution run (genuine sklearn RF training, a real
    agent, a real corrupted blob afterward): the generic per-part file loop in _group_result
    already excluded a corrupted part via its checkpoint hash, but _job_scores/_job_trees/
    _job_reports/_job_sim_results each independently re-fetched blob.data with no hash check at
    all - so a corrupted part's "trees"/"score"/"tests"/"results" still made it into the
    merge/average/report/aggregate even though the raw files were correctly excluded from the
    zip. All four now share _verified_results(), which does the check once."""
    from app.models import PoolBlob, TaskRequest
    from app.pool_routes import _job_reports, _job_scores, _job_sim_results, _job_trees
    from datetime import datetime, timezone

    student = make_user("ck7", "ck7@uog.edu.pk")
    now = datetime.now(timezone.utc)
    group = TaskRequest(user_id=student.id, task_type="job_python", mode="job_group",
                        status="group", chunk_id=1, message="checkpoint-corruption test",
                        required_cpu=1, required_ram_mb=256, created_at=now)
    db.session.add(group)
    db.session.flush()

    # A payload with every field each reducer looks for, so a false "excluded" pass can't come
    # from the field simply being absent rather than the hash check actually doing its job.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("output/result.json",
                  json.dumps({"score": 1, "trees": [{"a": 1}], "passed": 1, "failed": 0,
                              "tests": [{"name": "t"}], "results": [1.0, 2.0]}))
    payload = buf.getvalue()
    blob = PoolBlob(owner_user_id=student.id, kind="job_result", name="r.zip",
                    size=len(payload), data=payload)
    db.session.add(blob)
    db.session.flush()
    part = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="completed",
                       parent_task_id=group.id, chunk_id=1, result_blob_id=blob.id,
                       result_sha256="0" * 64,   # deliberately wrong, like a corrupted file
                       start_time=now, completed_at=now, exit_code=0,
                       required_cpu=1, required_ram_mb=256, created_at=now)
    db.session.add(part)
    db.session.commit()

    kids = [TaskRequest.query.get(part.id)]
    assert _job_scores(kids) == {}
    assert _job_trees(kids) == {}
    assert _job_reports(kids) == {}
    assert _job_sim_results(kids) == {}
