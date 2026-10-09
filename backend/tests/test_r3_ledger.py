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
    not a single free/used number."""
    from app.models import Node, TaskRequest

    admin = make_user("l2_admin", "l2admin@uog.edu.pk", role="admin")
    student = make_user("l2", "l2@uog.edu.pk")
    node = make_node("LEDGER-NODE", total_cores=8, total_ram_mb=8192, device_key_hash="k")

    db.session.add(TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
                               status="allocated", assigned_node_id=node.id,
                               required_cpu=2, required_ram_mb=2048,
                               created_at=datetime.now(timezone.utc)))
    db.session.add(TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
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
