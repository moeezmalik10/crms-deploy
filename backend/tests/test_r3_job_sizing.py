"""Per the proposal: a divisible job's units are "sized from the free memory reported in the
Unified Quota Ledger... so a unit always fits on the PC that receives it." Bounded to the plain
split path (kind="") - sweep's part count is one per parameter set and k-fold's is k folds, so
neither is a capacity choice to begin with; clamping either would silently drop a parameter set
or a fold, which is a correctness change, not a sizing one.

fitting_device_count() itself is covered directly; submit_job's use of it is covered through
the API so the actual stored TaskRequest.chunk_id count (not just an internal number) is what's
asserted.
"""
import io

from app.allocation_engine import fitting_device_count


def _upload_job(client, auth_headers, **form):
    return client.post("/pool/jobs", headers=auth_headers, data={
        "file": (io.BytesIO(b"print(1)"), "main.py"),
        "runtime": "python", "cores": "1", "ram_mb": "1024", "disk_mb": "50", "max_minutes": "1",
        **form,
    }, content_type="multipart/form-data")


def test_fitting_device_count_counts_only_devices_with_room(app, db, make_node):
    make_node("FIT-BIG", total_cores=8, total_ram_mb=8192, device_key_hash="f1", sandbox_mode="docker")
    make_node("FIT-SMALL", total_cores=1, total_ram_mb=1024, device_key_hash="f2", sandbox_mode="docker")
    make_node("FIT-NOKEY", total_cores=8, total_ram_mb=8192, sandbox_mode="docker")   # no device key - not poolable
    make_node("FIT-NOSANDBOX", total_cores=8, total_ram_mb=8192, device_key_hash="f3")   # can't run jobs

    with app.app_context():
        # 1 core / 2 GB fits only FIT-BIG - FIT-SMALL's 1 total core is entirely eaten by the
        # 5% safety buffer and 768 MB is reserved out of its 1 GB RAM before anything is asked for.
        assert fitting_device_count(1, 2048) == 1


def test_fitting_device_count_subtracts_reserved_and_used(app, db, make_node, make_user):
    from datetime import datetime, timezone
    from app.allocation_engine import allocate_task
    from app.models import TaskRequest

    make_node("FIT-TAKEN", total_cores=4, total_ram_mb=4096, device_key_hash="f4", sandbox_mode="docker",
             free_storage_gb=50)
    student = make_user("fit1", "fit1@uog.edu.pk")
    first = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="pending",
                        priority=50, required_cpu=3, required_ram_mb=3000,
                        created_at=datetime.now(timezone.utc))
    db.session.add(first)
    db.session.commit()

    with app.app_context():
        allocate_task(first.id)   # books most of FIT-TAKEN's capacity
        assert fitting_device_count(3, 3000) == 0   # nothing has room for another unit this size


def test_plain_split_job_is_clamped_to_devices_that_actually_fit(client, make_user, auth, make_node):
    """Three devices are online but only two have room for a 2-core/2GB unit - asking for 4
    parts must not create 4 TaskRequest children the pool has no way to place at submission
    time. Clamped to 2, not 1, so this stays a split job_group (parts == 1 collapses into a
    plain non-split task with no sub_tasks at all - a different code path, covered below)."""
    from app.models import TaskRequest

    make_node("SIZE-BIG-1", total_cores=8, total_ram_mb=8192, device_key_hash="s1", sandbox_mode="docker",
             free_storage_gb=50)
    make_node("SIZE-BIG-2", total_cores=8, total_ram_mb=8192, device_key_hash="s1b", sandbox_mode="docker",
             free_storage_gb=50)
    make_node("SIZE-SMALL", total_cores=1, total_ram_mb=1024, device_key_hash="s2", sandbox_mode="docker",
             free_storage_gb=50)
    student = make_user("sz1", "sz1@uog.edu.pk")

    r = _upload_job(client, auth(student), parts="4", cores="2", ram_mb="2048")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert len(group.sub_tasks) == 2   # clamped down to the two devices that actually fit


def test_plain_split_job_clamped_to_one_device_submits_as_a_single_task(client, make_user, auth, make_node):
    """When the clamp lands on exactly 1, submit_job's parts==1 path takes over and creates a
    plain non-split task instead of a one-child group - still correct, just a different shape
    of "one part" than a job_group with one sub_task."""
    from app.models import TaskRequest

    make_node("SIZE-ONLY-BIG", total_cores=8, total_ram_mb=8192, device_key_hash="s3", sandbox_mode="docker",
             free_storage_gb=50)
    make_node("SIZE-ONLY-SMALL", total_cores=1, total_ram_mb=1024, device_key_hash="s4", sandbox_mode="docker",
             free_storage_gb=50)
    student = make_user("sz1b", "sz1b@uog.edu.pk")

    r = _upload_job(client, auth(student), parts="4", cores="2", ram_mb="2048")
    assert r.status_code == 200
    task = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert task.mode == "job" and not task.sub_tasks


def test_plain_split_job_keeps_the_requested_count_when_it_all_fits(client, make_user, auth, make_node):
    from app.models import TaskRequest

    for i in range(3):
        make_node(f"SIZE-OK-{i}", total_cores=8, total_ram_mb=8192, device_key_hash=f"ok{i}", sandbox_mode="docker")
    student = make_user("sz2", "sz2@uog.edu.pk")

    r = _upload_job(client, auth(student), parts="3", cores="1", ram_mb="512")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert len(group.sub_tasks) == 3


def test_plain_split_job_falls_back_to_the_requested_count_with_no_devices_online(client, make_user, auth):
    """No online device at all shouldn't make submission impossible - the job queues normally
    and waits, same as it always has; fitting_device_count returning 0 must not be read as
    "clamp to zero parts"."""
    from app.models import TaskRequest

    student = make_user("sz3", "sz3@uog.edu.pk")
    r = _upload_job(client, auth(student), parts="3", cores="1", ram_mb="512")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert len(group.sub_tasks) == 3


def test_sweep_part_count_is_never_clamped_by_capacity(client, make_user, auth, make_node):
    """A sweep's part count means one part per parameter set - clamping it would silently
    drop a requested parameter set, which is a correctness bug, not a sizing optimization."""
    from app.models import TaskRequest

    make_node("SIZE-TINY", total_cores=1, total_ram_mb=512, device_key_hash="sw1", sandbox_mode="docker")
    student = make_user("sz4", "sz4@uog.edu.pk")

    r = _upload_job(client, auth(student), kind="sweep", cores="1", ram_mb="256",
                    param_sets="--lr 0.01\n--lr 0.1\n--lr 0.5")
    assert r.status_code == 200
    group = TaskRequest.query.get(r.get_json()["job"]["id"])
    assert len(group.sub_tasks) == 3
