"""Submits a real Random Forest job, waits for the real agent to actually train and report it,
then checks the whole pipeline against genuine output: per-part checkpoint hashes, the merged
forest's tree count, and - by deliberately corrupting one stored part afterward - that a
real corrupted part is excluded from the combined download rather than silently included.
"""
import hashlib
import io
import json
import time
import zipfile

import requests

import common
common.configure_env()   # needed before importing app.* below

API = requests.Session()


def login():
    r = API.post(f"{common.BASE_URL}/auth/login",
                 json={"email": common.STUDENT_EMAIL, "password": common.STUDENT_PASSWORD})
    r.raise_for_status()
    API.headers["Authorization"] = "Bearer " + r.json()["access_token"]


def submit_rf_job(parts=2):
    data_csv = (
        "f1,f2,f3,target\n"
        "1,2,3,0\n2,3,4,0\n3,4,5,0\n1,1,1,0\n2,1,2,0\n"
        "9,8,7,1\n8,7,6,1\n7,6,5,1\n9,9,9,1\n8,9,7,1\n"
    )
    with open("rf_train.py", "rb") as f:
        program = f.read()
    r = API.post(f"{common.BASE_URL}/pool/jobs", data={
        "runtime": "python", "cores": "1", "ram_mb": "512", "disk_mb": "100", "max_minutes": "1",
        "kind": "rf", "parts": str(parts),
    }, files={"file": ("main.py", program, "text/x-python"),
              "data": ("data.csv", data_csv.encode(), "text/csv")})
    r.raise_for_status()
    return r.json()["job"]["id"]


def wait_for_group(group_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = API.get(f"{common.BASE_URL}/pool/jobs")
        r.raise_for_status()
        job = next((j for j in r.json() if j["id"] == group_id), None)
        if job and job["status"] in ("completed", "failed"):
            return job
        time.sleep(1)
    raise TimeoutError("job did not finish in time")


def main():
    login()
    group_id = submit_rf_job(parts=2)
    print(f"Submitted RF job group #{group_id}, waiting for real execution...")
    job = wait_for_group(group_id)
    print(f"Group status: {job['status']}")
    assert job["status"] == "completed", f"job did not complete cleanly: {job}"

    print("\nPer-part checkpoints:")
    for p in job["parts"]:
        ck = p.get("checkpoint")
        print(f"  part {p['part']}: status={p['status']}, exit_code={p['exit_code']}, "
             f"checkpoint sha256={ck['sha256'][:16] + '...' if ck else None}")
        assert ck is not None, f"part {p['part']} has no checkpoint recorded"

    assert job.get("trees_total"), f"no merged tree count reported: {job}"
    print(f"\nReported trees_total={job['trees_total']}, groups_merged={job['groups_merged']}")

    r = API.get(f"{common.BASE_URL}/pool/jobs/{group_id}/result")
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        combined = json.loads(z.read("combined_forest.json"))
        summary = z.read("summary.txt").decode()
    print(f"\ncombined_forest.json: n_estimators={combined['n_estimators']}")
    assert combined["n_estimators"] == job["trees_total"]
    assert len(combined["trees"]) == combined["n_estimators"]
    # Each tree is the real JSON walked out of a genuine sklearn DecisionTreeClassifier -
    # confirm the shape, not a stub.
    first_tree = combined["trees"][0]
    for key in ("feature", "threshold", "children_left", "children_right", "value"):
        assert key in first_tree, f"tree is missing real sklearn structure: {first_tree.keys()}"
    print("Combined forest trees have genuine sklearn tree structure (not a stub).")

    print("\n--- Corruption test: flipping one stored part's bytes after the fact ---")
    from app.models import PoolBlob, TaskRequest
    from app import db, create_app
    app = create_app()
    with app.app_context():
        group = TaskRequest.query.get(group_id)
        victim = sorted(group.sub_tasks, key=lambda c: c.chunk_id)[0]
        blob = PoolBlob.query.get(victim.result_blob_id)
        original_hash = victim.result_sha256
        blob.data = blob.data[:-1] + bytes([blob.data[-1] ^ 0xFF])   # flip the last byte
        db.session.commit()
        print(f"Corrupted stored blob for part {victim.chunk_id} "
             f"(checkpoint hash on file: {original_hash[:16]}...)")

    r2 = API.get(f"{common.BASE_URL}/pool/jobs/{group_id}/result")
    r2.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r2.content)) as z:
        combined2 = json.loads(z.read("combined_forest.json"))
        summary2 = z.read("summary.txt").decode()
    assert combined2["n_estimators"] < combined["n_estimators"], (
        "corrupted part's trees should have been excluded from the re-combined download")
    assert "result check failed" in summary2
    print(f"After corruption: n_estimators={combined2['n_estimators']} "
         f"(was {combined['n_estimators']}) - corrupted part correctly excluded.")
    print("\nsummary.txt after corruption:\n" + summary2)

    print("\nCleaning up test job...")
    API.delete(f"{common.BASE_URL}/pool/jobs/{group_id}")
    print("ALL CHECKS PASSED.")


if __name__ == "__main__":
    main()
