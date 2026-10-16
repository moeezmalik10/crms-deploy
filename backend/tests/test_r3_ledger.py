"""The resource-pool ledger (GET /pool/ledger) and the metrics-history trail.

NOTE: lock_online_nodes()/the with_for_update() calls in allocate_task, _storage_targets and
spawn_distributed_ml are exercised here only for their single-request behavior. SQLite has no
row-level locking, so the actual concurrency guarantee ("no over-allocation under concurrent
requests") is NOT proven by anything in this file - that needs a real Postgres instance.
"""
from datetime import datetime, timedelta, timezone


def test_ledger_requires_admin(client, make_user, auth):
    student = make_user("l1", "l1@uog.edu.pk")
    r = client.get("/pool/ledger", headers=auth(student))
    assert r.status_code == 403


def test_ledger_separates_reserved_from_in_use(client, make_user, auth, make_node, db):
    """The three-way split the proposal's pool manager is built around: installed capacity,
    "reserved" (promised to a task that hasn't started yet), and "in_use" (actually running) -
    not a single free/used number. Divisible job-mode work books only its own profile, since
    several jobs can share one device."""
    from app.models import Node, TaskRequest

    admin = make_user("l2_admin", "l2admin@uog.edu.pk", role="admin")
    student = make_user("l2", "l2@uog.edu.pk")
    node = make_node("LEDGER-NODE", total_cores=8, total_ram_mb=8192, device_key_hash="k")

    db.session.add(TaskRequest(user_id=student.id, task_type="job_python", mode="job",
                               status="allocated", assigned_node_id=node.id,
                               required_cpu=2, required_ram_mb=2048,
                               created_at=datetime.now(timezone.utc)))
    db.session.add(TaskRequest(user_id=student.id, task_type="job_python", mode="job",
                               status="running", assigned_node_id=node.id,
                               required_cpu=1, required_ram_mb=1024,
                               created_at=datetime.now(timezone.utc)))
    db.session.commit()

    r = client.get("/pool/ledger", headers=auth(admin))
    assert r.status_code == 200
    row = next(d for d in r.get_json() if d["id"] == node.id)
    assert row["reserved_cpu"] == 2 and row["reserved_ram_mb"] == 2048
    assert row["in_use_cpu"] == 1 and row["in_use_ram_mb"] == 1024
    assert row["free_cpu"] == 8 - 2 - 1


def test_ledger_books_a_physical_session_as_the_whole_device(client, make_user, auth, make_node, db):
    """Rule A gives a physical session the entire PC, not just its own modest profile (e.g. a
    dev_cpp session only asks for 1 core/2 GB) - so the ledger must show the device as fully
    booked while one is active, or it would advertise free capacity the scheduler can never
    actually hand out (no other task is ever placed alongside a physical session)."""
    from app.models import TaskRequest

    admin = make_user("l3_admin", "l3admin@uog.edu.pk", role="admin")
    student = make_user("l3", "l3@uog.edu.pk")
    node = make_node("PHYS-NODE", total_cores=8, total_ram_mb=8192, device_key_hash="k2")

    db.session.add(TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
                               status="running", assigned_node_id=node.id,
                               required_cpu=1, required_ram_mb=2048,
                               created_at=datetime.now(timezone.utc)))
    db.session.commit()

    r = client.get("/pool/ledger", headers=auth(admin))
    row = next(d for d in r.get_json() if d["id"] == node.id)
    assert row["in_use_cpu"] == 8 and row["in_use_ram_mb"] == 8192
    assert row["reserved_cpu"] == 0
    assert row["free_cpu"] == 0 and row["free_ram_mb"] == 0


def test_heartbeat_samples_metrics_history_at_most_every_five_minutes(client, make_node, db):
    """The bug the history table fixes: NodeMetrics itself is upserted (one row per node), so
    it has no history. The sampler must not record a row on every 30s heartbeat either, or the
    table would grow unmanageably fast."""
    from app.models import Node, NodeMetricsHistory
    from app.pool_security import sha256_hex

    node = make_node("HIST-NODE", device_key_hash=sha256_hex("hk1"))

    def heartbeat():
        return client.post("/agent/heartbeat", headers={"X-Device-Key": "hk1"}, json={
            "host": {"cpu": {"used_percent": 10}, "memory": {"free": 1024 * 1024 * 1024, "total": 2 * 1024**3},
                     "storage": {"total": 100 * 1024**3, "free": 50 * 1024**3}}
        })

    r1 = heartbeat()
    assert r1.status_code == 200
    assert NodeMetricsHistory.query.filter_by(node_id=node.id).count() == 1

    r2 = heartbeat()   # immediately again - should NOT add a second row
    assert r2.status_code == 200
    assert NodeMetricsHistory.query.filter_by(node_id=node.id).count() == 1

    # Backdate the one sample so it looks like it happened 6 minutes ago, then heartbeat again.
    sample = NodeMetricsHistory.query.filter_by(node_id=node.id).first()
    sample.timestamp = datetime.now(timezone.utc) - timedelta(minutes=6)
    db.session.commit()

    r3 = heartbeat()
    assert r3.status_code == 200
    assert NodeMetricsHistory.query.filter_by(node_id=node.id).count() == 2


def test_allocate_task_persists_the_ledger_onto_the_node_row(app, db, make_user, make_node):
    """refresh_ledger() (called from allocate_task) must actually write Node.used_cores/
    reserved_cores/used_ram_mb/reserved_ram_mb, not just return a computed dict - that's the
    whole point of a persisted Unified Quota Ledger rather than the old on-the-fly-only view."""
    from app.allocation_engine import allocate_task
    from app.models import Node, TaskRequest

    node = make_node("PERSIST-NODE", total_cores=8, total_ram_mb=8192, device_key_hash="pk1",
                     sandbox_mode="docker", free_storage_gb=50)
    student = make_user("l4", "l4@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="job_python", mode="job", status="pending",
                       priority=50, required_cpu=2, required_ram_mb=2048, required_disk_mb=100,
                       created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()

    with app.app_context():
        result = allocate_task(task.id)
    assert result["status"] == "allocated"

    db.session.expire_all()
    n = Node.query.get(node.id)
    assert n.reserved_cores == 2 and n.reserved_ram_mb == 2048   # "allocated", not yet running
    assert n.used_cores == 0 and n.used_ram_mb == 0


def test_the_ledger_check_constraint_rejects_over_booking(app, db, make_node):
    """A direct, deliberately-wrong write (bypassing allocate_task's own bookkeeping entirely)
    must still be rejected by the database itself - that's the actual value of a DB-level CHECK
    over an application-level invariant: it holds even when the application code is wrong.
    Confirmed this constraint is what's doing the work (not some other validation) by checking
    it raises specifically when used+reserved exceeds total, and not for a legal value."""
    from sqlalchemy.exc import IntegrityError
    from app.models import Node

    node = make_node("CHECK-NODE", total_cores=4, total_ram_mb=4096)

    node.used_cores = 4
    node.reserved_cores = 1   # 5 > 4 total - must be rejected
    db.session.add(node)
    try:
        db.session.commit()
        assert False, "expected the CHECK constraint to reject used+reserved > total"
    except IntegrityError:
        db.session.rollback()

    db.session.expire_all()
    n = Node.query.get(node.id)
    n.used_cores = 4
    n.reserved_cores = 0   # exactly at total - legal
    db.session.commit()
    db.session.expire_all()
    assert Node.query.get(node.id).used_cores == 4


def test_trim_metrics_history_keeps_only_seven_days(app, db, make_node):
    from app.models import NodeMetricsHistory
    from app.tasks import trim_metrics_history

    node = make_node("TRIM-NODE")
    now = datetime.now(timezone.utc)
    db.session.add(NodeMetricsHistory(node_id=node.id, cpu_used=1, timestamp=now - timedelta(days=10)))
    db.session.add(NodeMetricsHistory(node_id=node.id, cpu_used=1, timestamp=now - timedelta(days=1)))
    db.session.commit()
    assert NodeMetricsHistory.query.count() == 2

    with app.app_context():
        trim_metrics_history()
        db.session.expire_all()

    rows = NodeMetricsHistory.query.all()
    assert len(rows) == 1
    assert (now - rows[0].timestamp.replace(tzinfo=timezone.utc)) < timedelta(days=7)
