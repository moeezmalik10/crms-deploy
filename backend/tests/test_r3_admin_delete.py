"""delete_user and delete_node used to throw a raw 500 (IntegrityError) the moment the user
or node had any history at all - found live in production while cleaning up test data (see
session notes). These tests run with sqlite's own foreign-key enforcement turned on (see
conftest.py) specifically so they'd catch the original bug for real, not just pass regardless.
"""
from datetime import datetime, timezone

from app.models import (DeviceEnrollment, MLResult, Node, NodeMetrics, NodeMetricsHistory,
                        PoolBlob, PoolChunk, PoolFile, PoolReplica, TaskExecutionLog,
                        TaskRequest, User)
from app import db


def test_delete_user_with_task_and_log_history(client, make_user, auth, db):
    student = make_user("delu1", "delu1@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical", status="completed",
                       created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.flush()
    db.session.add(TaskExecutionLog(task_id=task.id, node_id=None, status="completed",
                                    message="done", timestamp=datetime.now(timezone.utc)))
    db.session.add(MLResult(task_id=task.id, accuracy=0.9))
    db.session.commit()
    task_id = task.id   # captured now - the row (and this stale object) won't exist after the delete

    admin = make_user("delu1_admin", "delu1admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/users/{student.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 200, r.get_json()
    assert User.query.get(student.id) is None
    assert TaskRequest.query.filter_by(user_id=student.id).count() == 0
    assert TaskExecutionLog.query.filter_by(task_id=task_id).count() == 0
    assert MLResult.query.filter_by(task_id=task_id).count() == 0


def test_delete_user_orphans_a_contributed_device_instead_of_deleting_it(client, make_user, auth, make_node, db):
    student = make_user("delu2", "delu2@uog.edu.pk")
    node = make_node("DELU-NODE", device_key_hash="k", owner_user_id=student.id)

    admin = make_user("delu2_admin", "delu2admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/users/{student.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 200, r.get_json()

    db.session.expire_all()
    still_there = Node.query.get(node.id)
    assert still_there is not None, "a contributed device should not be deleted along with its owner"
    assert still_there.owner_user_id is None


def test_delete_user_clears_join_code_history(client, make_user, auth, db):
    student = make_user("delu3", "delu3@uog.edu.pk")
    db.session.add(DeviceEnrollment(user_id=student.id, code_hash="x", expires_at=datetime.now(timezone.utc)))
    db.session.commit()

    admin = make_user("delu3_admin", "delu3admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/users/{student.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 200, r.get_json()
    assert DeviceEnrollment.query.filter_by(user_id=student.id).count() == 0


def test_delete_user_blocked_while_pool_file_still_has_a_stored_replica(client, make_user, auth, make_node, db):
    """A file with a live "stored" replica can't just vanish - that's a real encrypted copy
    sitting on a contributor's disk that needs to be told to clean up first (the async
    pool_storage_upkeep path), not silently orphaned."""
    student = make_user("delu4", "delu4@uog.edu.pk")
    other_node = make_node("DELU-HOLDER", device_key_hash="h")
    f = PoolFile(owner_user_id=student.id, name="secret.txt", size=10, sha256="x", status="stored")
    db.session.add(f)
    db.session.flush()
    chunk = PoolChunk(file_id=f.id, idx=0, size=10, sha256="x")
    db.session.add(chunk)
    db.session.flush()
    db.session.add(PoolReplica(chunk_id=chunk.id, node_id=other_node.id, status="stored"))
    db.session.commit()

    admin = make_user("delu4_admin", "delu4admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/users/{student.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 409
    assert "secret.txt" in r.get_json()["error"]
    # Nothing committed - the user and their file must still exist, untouched.
    db.session.expire_all()
    assert User.query.get(student.id) is not None
    assert PoolFile.query.get(f.id) is not None


def test_delete_user_succeeds_once_pool_file_has_no_live_replicas(client, make_user, auth, db):
    """A file whose only replica was still "pending" (never actually stored anywhere) has
    nothing real to clean up - it can go immediately."""
    student = make_user("delu5", "delu5@uog.edu.pk")
    f = PoolFile(owner_user_id=student.id, name="empty.txt", size=10, sha256="x", status="replicating")
    db.session.add(f)
    db.session.commit()

    admin = make_user("delu5_admin", "delu5admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/users/{student.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 200, r.get_json()
    assert PoolFile.query.get(f.id) is None


def test_delete_node_with_historical_non_running_task(client, make_user, auth, make_node, db):
    """The bug: delete_node only checked for a status=='running' task and otherwise deleted
    the row unconditionally - a *completed* task still referencing this node via
    assigned_node_id (nullable, but RESTRICT by default) made the delete 500."""
    node = make_node("DELN-1", device_key_hash="k")
    student = make_user("deln1", "deln1@uog.edu.pk")
    task = TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical", status="completed",
                       assigned_node_id=node.id, assigned_pc=node.name, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.commit()
    task_id = task.id

    admin = make_user("deln1_admin", "deln1admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/nodes/{node.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 200, r.get_json()
    assert Node.query.get(node.id) is None

    db.session.expire_all()
    t = TaskRequest.query.get(task_id)
    assert t is not None, "the task's own history should survive - only the node link is dropped"
    assert t.assigned_node_id is None
    assert t.assigned_pc == "DELN-1"   # the device's name is kept as a plain-text record


def test_delete_node_blocked_while_a_task_is_allocated_not_just_running(client, make_user, auth, make_node, db):
    """The bug: only status=='running' was checked, so a task in 'allocated' or 'starting'
    (assigned moments ago, agent hasn't polled yet) didn't block the delete and would have
    been silently orphaned instead."""
    node = make_node("DELN-2", device_key_hash="k")
    student = make_user("deln2", "deln2@uog.edu.pk")
    db.session.add(TaskRequest(user_id=student.id, task_type="dev_cpp", mode="physical",
                               status="allocated", assigned_node_id=node.id, assigned_pc=node.name,
                               created_at=datetime.now(timezone.utc)))
    db.session.commit()

    admin = make_user("deln2_admin", "deln2admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/nodes/{node.id}", headers=auth(admin, role="admin"))
    assert r.status_code == 400
    db.session.expire_all()
    assert Node.query.get(node.id) is not None


def test_delete_node_with_pool_replica_and_metrics_history(client, make_user, auth, make_node, db):
    owner = make_user("deln3_owner", "deln3owner@uog.edu.pk")
    node = make_node("DELN-3", device_key_hash="k")
    f = PoolFile(owner_user_id=owner.id, name="x.txt", size=1, sha256="x", status="stored")
    db.session.add(f)
    db.session.flush()
    chunk = PoolChunk(file_id=f.id, idx=0, size=1, sha256="x")
    db.session.add(chunk)
    db.session.flush()
    db.session.add(PoolReplica(chunk_id=chunk.id, node_id=node.id, status="stored"))
    db.session.add(NodeMetrics(node_id=node.id, cpu_used=1))
    db.session.add(NodeMetricsHistory(node_id=node.id, cpu_used=1))
    db.session.add(DeviceEnrollment(user_id=owner.id, code_hash="y", expires_at=datetime.now(timezone.utc),
                                    node_id=node.id))
    db.session.commit()
    node_id = node.id

    admin = make_user("deln3_admin", "deln3admin@uog.edu.pk", role="admin")
    r = client.delete(f"/admin/nodes/{node_id}", headers=auth(admin, role="admin"))
    assert r.status_code == 200, r.get_json()
    assert Node.query.get(node_id) is None
    assert PoolReplica.query.filter_by(node_id=node_id).count() == 0
    assert NodeMetrics.query.filter_by(node_id=node_id).count() == 0
    assert NodeMetricsHistory.query.filter_by(node_id=node_id).count() == 0
