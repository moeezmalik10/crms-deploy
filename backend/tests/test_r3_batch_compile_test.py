"""The fourth defined batch job type: batch compile-and-test. Per the proposal (Table 4): split
by test cases, combined by collecting reports. Unlike k-fold/RF, test cases are ROW-SLICED
across parts (same mechanism as the plain split job) - the point is running a fixed, slow test
suite faster by spreading it across devices, not training on the whole thing in every part.
"""
import io
import json
import zipfile


def _upload_compile_test_job(client, auth_headers, parts="3", **form):
    return client.post("/pool/jobs", headers=auth_headers, data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "data": (io.BytesIO(b"test_name\ntest_add\ntest_sub\ntest_mul\ntest_div\ntest_neg\ntest_zero\n"), "tests.csv"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "compile_test", "parts": parts,
        **form,
    }, content_type="multipart/form-data")


def test_compile_test_needs_a_data_file(client, make_user, auth):
    student = make_user("ct1", "ct1@uog.edu.pk")
    r = client.post("/pool/jobs", headers=auth(student), data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "128", "disk_mb": "50", "max_minutes": "1",
        "kind": "compile_test", "parts": "3",
    }, content_type="multipart/form-data")
    assert r.status_code == 400
    assert "data file" in r.get_json()["error"]


def test_compile_test_splits_test_cases_by_rows_not_whole_file(client, make_user, auth, db):
    """Unlike k-fold/RF, each part gets its OWN slice of test cases, not the whole file."""
    from app.models import PoolBlob, TaskRequest

    student = make_user("ct2", "ct2@uog.edu.pk")
    r = _upload_compile_test_job(client, auth(student), parts="3")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert group.job_kind == "compile_test"
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)
    assert len(kids) == 3

    seen_rows = []
    for c in kids:
        blob = PoolBlob.query.get(c.input_blob_id)
        files = zipfile.ZipFile(io.BytesIO(blob.data))
        text = files.read("tests.csv").decode()
        rows = [l for l in text.splitlines() if l and l != "test_name"]
        seen_rows.extend(rows)
        assert len(rows) < 6   # no part has every test case - it was split, not duplicated
    assert len(seen_rows) == 6   # but together they cover all of them


def _fake_report_blob(passed, failed, tests):
    from app.models import PoolBlob
    from app import db
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("result.json", json.dumps({"passed": passed, "failed": failed, "tests": tests}))
    blob = PoolBlob(kind="job_result", name="r.zip", size=buf.tell(), data=buf.getvalue())
    db.session.add(blob)
    db.session.flush()
    return blob.id


def test_compile_test_group_json_sums_reports_across_parts(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_json
    from datetime import datetime, timezone

    student = make_user("ct3", "ct3@uog.edu.pk")
    r = _upload_compile_test_job(client, auth(student), parts="3")
    group_id = r.get_json()["job"]["id"]
    group = TaskRequest.query.get(group_id)
    kids = sorted(group.sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    reports = [
        (3, 0, [{"name": "test_add", "passed": True}]),
        (2, 1, [{"name": "test_div", "passed": False}]),
        (1, 1, [{"name": "test_neg", "passed": False}]),
    ]
    for c, (passed, failed, tests) in zip(kids, reports):
        c.status = "completed"
        c.start_time = now
        c.completed_at = now
        c.result_blob_id = _fake_report_blob(passed, failed, tests)
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["status"] == "completed"
    assert j["tests_passed"] == 6
    assert j["tests_failed"] == 2
    assert j["tests_total"] == 8
    assert j["all_passed"] is False


def test_compile_test_group_json_reports_all_passed_when_nothing_failed(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_json
    from datetime import datetime, timezone

    student = make_user("ct4", "ct4@uog.edu.pk")
    r = _upload_compile_test_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    kids = sorted(TaskRequest.query.get(group_id).sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    for c, passed in zip(kids, [2, 3]):
        c.status, c.start_time, c.completed_at = "completed", now, now
        c.result_blob_id = _fake_report_blob(passed, 0, [])
    db.session.commit()

    j = _group_json(TaskRequest.query.get(group_id))
    assert j["all_passed"] is True
    assert j["tests_failed"] == 0


def test_compile_test_group_result_zip_collects_the_combined_report(client, make_user, auth, db):
    from app.models import TaskRequest
    from app.pool_routes import _group_result, _read_zip
    from datetime import datetime, timezone

    student = make_user("ct5", "ct5@uog.edu.pk")
    r = _upload_compile_test_job(client, auth(student), parts="2")
    group_id = r.get_json()["job"]["id"]
    kids = sorted(TaskRequest.query.get(group_id).sub_tasks, key=lambda c: c.chunk_id)

    now = datetime.now(timezone.utc)
    kids[0].status, kids[0].start_time, kids[0].completed_at = "completed", now, now
    kids[0].result_blob_id = _fake_report_blob(2, 0, [{"name": "test_add", "passed": True},
                                                      {"name": "test_sub", "passed": True}])
    kids[1].status, kids[1].start_time, kids[1].completed_at = "completed", now, now
    kids[1].result_blob_id = _fake_report_blob(0, 1, [{"name": "test_div", "passed": False}])
    db.session.commit()

    resp = _group_result(TaskRequest.query.get(group_id))
    files = _read_zip(resp.get_data())
    summary = files["summary.txt"].decode()
    assert "2 passed, 1 failed, across 2 of 2 part(s)" in summary

    combined = json.loads(files["combined_report.json"])
    assert combined["passed"] == 2 and combined["failed"] == 1
    assert combined["tests"] == [{"name": "test_add", "passed": True},
                                 {"name": "test_sub", "passed": True},
                                 {"name": "test_div", "passed": False}]
