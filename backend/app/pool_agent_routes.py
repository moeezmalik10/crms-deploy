"""Agent-side endpoints of the resource pool: joining, compute-job files, storage chunks.

Every route except /agent/join needs the device key (X-Device-Key). The agent only ever
makes outgoing HTTPS calls to these routes, so a contributed PC opens no ports.
"""
import hashlib
from datetime import datetime, timezone, timedelta

from flask import Blueprint, Response, g, jsonify, request

from app import db
from app.allocation_engine import allocate_task, log_event
from app.models import DeviceEnrollment, Node, PoolBlob, PoolChunk, PoolFile, PoolReplica, TaskRequest
from app.pool_security import agent_auth, new_device_key, sha256_hex, client_public_ip

pool_agent_bp = Blueprint("pool_agent", __name__)

MAX_RESULT_BYTES = 20 * 1024 * 1024


def _keyed_only():
    if g.node is None:
        return jsonify({"error": "device key required - join the pool from the website"}), 401
    return None


# ---------------------------------------------------------------- joining
@pool_agent_bp.route("/agent/join", methods=["POST"])
def agent_join():
    """Exchange a one-time join code (made on the website) for a permanent device key."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    # Lock the row for the rest of this transaction so two concurrent joins with the
    # same one-time code can't both read used_at as unset before either commits.
    enr = DeviceEnrollment.query.filter_by(code_hash=sha256_hex(code)).with_for_update().first()
    now = datetime.now(timezone.utc)
    if not enr or enr.used_at:
        return jsonify({"error": "join code is not valid or was already used - make a new one on the website"}), 400
    expires = enr.expires_at if enr.expires_at.tzinfo else enr.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        return jsonify({"error": "join code expired - make a new one on the website"}), 400

    hostname = (data.get("hostname") or "DEVICE").strip()[:60] or "DEVICE"
    name = hostname
    existing = Node.query.filter_by(name=name).first()
    # Only a device this same student already owns may be reused by name - a node
    # with no owner (e.g. a lab PC registered through the old /register_node flow,
    # or any other contributor's device) must never be silently taken over.
    if existing and existing.owner_user_id != enr.user_id:
        n = 2
        while Node.query.filter_by(name=f"{hostname}-{n}").first():
            n += 1
        name = f"{hostname}-{n}"
        existing = None

    node = existing or Node(name=name)
    if not existing:
        db.session.add(node)
    key = new_device_key()
    node.device_key_hash = sha256_hex(key)
    node.owner_user_id = enr.user_id
    node.share_cores = enr.share_cores
    node.share_ram_mb = enr.share_ram_mb
    node.share_storage_gb = enr.share_storage_gb
    node.allow_light_sandbox = bool(enr.allow_light_sandbox)
    node.paused = False
    node.total_cores = int(data.get("total_cores") or 0)
    node.total_ram_mb = float(data.get("total_ram_mb") or 0)
    node.status = "online"
    node.last_heartbeat = now
    node.public_ip = client_public_ip()[:64]
    db.session.flush()
    enr.used_at = now
    enr.node_id = node.id
    db.session.commit()
    return jsonify({"device_key": key, "node_name": node.name, "node_id": node.id}), 200


# ---------------------------------------------------------------- compute jobs
@pool_agent_bp.route("/agent/jobs/<int:task_id>/input", methods=["GET"])
@agent_auth()
def job_input(task_id):
    err = _keyed_only()
    if err: return err
    task = TaskRequest.query.get_or_404(task_id)
    if task.assigned_node_id != g.node.id or task.mode != "job":
        return jsonify({"error": "not your job"}), 403
    blob = PoolBlob.query.get_or_404(task.input_blob_id)
    return Response(blob.data, mimetype="application/octet-stream",
                    headers={"X-File-Name": blob.name or "input"})


@pool_agent_bp.route("/agent/jobs/<int:task_id>/result", methods=["POST"])
@agent_auth()
def job_result(task_id):
    err = _keyed_only()
    if err: return err
    task = TaskRequest.query.get_or_404(task_id)
    if task.assigned_node_id != g.node.id or task.mode != "job":
        return jsonify({"error": "not your job"}), 403
    if task.status not in ("running", "starting"):
        return jsonify({"status": "ignored", "reason": f"job is {task.status}"}), 200

    exit_code = request.form.get("exit_code", type=int)
    reason = (request.form.get("reason") or "").strip()
    task.exit_code = exit_code
    task.output_tail = (request.form.get("output") or "")[-60000:]
    f = request.files.get("result")
    if f:
        data = f.read(MAX_RESULT_BYTES + 1)
        if len(data) > MAX_RESULT_BYTES:
            data = b""
            reason = reason or "result too large (over 20 MB)"
        if data:
            blob = PoolBlob(owner_user_id=task.user_id, kind="job_result",
                            name=f"job_{task.id}_result.zip", size=len(data), data=data)
            db.session.add(blob)
            db.session.flush()
            task.result_blob_id = blob.id
    ok = exit_code == 0 and not reason
    task.status = "completed" if ok else "failed"
    task.completed_at = datetime.now(timezone.utc)
    msg = f"Job finished on {g.node.name} (exit code {exit_code})" + (f": {reason}" if reason else "")
    task.message = msg
    log_event(task.id, g.node.id, task.status, msg)
    db.session.commit()

    # The device has room again: hand it the next waiting part of a split job straight
    # away instead of waiting for the next queue round.
    if task.parent_task_id:
        waiting = TaskRequest.query.filter_by(parent_task_id=task.parent_task_id, status="pending") \
                                   .order_by(TaskRequest.chunk_id.asc()).first()
        if waiting:
            try:
                allocate_task(waiting.id)
            except Exception as e:   # the regular queue round will retry
                db.session.rollback()
                print(f"[pool] could not place part {waiting.chunk_id} now: {e}")
    return jsonify({"status": task.status}), 200


# ---------------------------------------------------------------- pooled storage
@pool_agent_bp.route("/agent/storage/tasks", methods=["GET"])
@agent_auth()
def storage_tasks():
    """What this device should do with storage chunks right now."""
    err = _keyed_only()
    if err: return err
    if g.node.paused:
        return jsonify({"tasks": []})
    out = []
    reps = PoolReplica.query.filter_by(node_id=g.node.id).filter(
        PoolReplica.status.in_(["pending", "deleting"])).limit(20).all()
    for r in reps:
        c = r.chunk
        if r.status == "pending" and c.tmp_blob_id:
            out.append({"action": "store", "replica_id": r.id, "chunk_id": c.id, "sha256": c.sha256, "size": c.size})
        elif r.status == "deleting":
            out.append({"action": "delete", "replica_id": r.id, "chunk_id": c.id})
    # Chunks the server needs back from this device: someone is downloading the file, or a
    # copy was lost (device revoked) and a new copy must be made on another device.
    held = (db.session.query(PoolChunk)
            .join(PoolReplica, PoolReplica.chunk_id == PoolChunk.id)
            .filter(PoolChunk.tmp_blob_id.is_(None),
                    PoolReplica.node_id == g.node.id,
                    PoolReplica.status == "stored")
            .limit(200).all())
    for c in held:
        f = c.file
        if f.status == "deleting":
            continue
        stored = sum(1 for r in c.replicas if r.status == "stored")
        if f.download_requested_at is not None or stored < (f.replicas_wanted or 1):
            out.append({"action": "send", "chunk_id": c.id, "sha256": c.sha256})
        if len(out) >= 30:
            break
    return jsonify({"tasks": out})


@pool_agent_bp.route("/agent/storage/chunks/<int:chunk_id>", methods=["GET"])
@agent_auth()
def storage_chunk_get(chunk_id):
    err = _keyed_only()
    if err: return err
    rep = PoolReplica.query.filter_by(chunk_id=chunk_id, node_id=g.node.id).first()
    if not rep:
        return jsonify({"error": "chunk not assigned to this device"}), 403
    chunk = PoolChunk.query.get_or_404(chunk_id)
    blob = PoolBlob.query.get(chunk.tmp_blob_id) if chunk.tmp_blob_id else None
    if not blob:
        return jsonify({"error": "chunk no longer on the server"}), 404
    return Response(blob.data, mimetype="application/octet-stream")


@pool_agent_bp.route("/agent/storage/replicas/<int:replica_id>/stored", methods=["POST"])
@agent_auth()
def storage_replica_stored(replica_id):
    err = _keyed_only()
    if err: return err
    rep = PoolReplica.query.get_or_404(replica_id)
    if rep.node_id != g.node.id:
        return jsonify({"error": "not your replica"}), 403
    rep.status = "stored"
    rep.updated_at = datetime.now(timezone.utc)
    db.session.flush()
    _maybe_finish_replication(rep.chunk.file)
    db.session.commit()
    return jsonify({"status": "stored"})


@pool_agent_bp.route("/agent/storage/replicas/<int:replica_id>/deleted", methods=["POST"])
@agent_auth()
def storage_replica_deleted(replica_id):
    err = _keyed_only()
    if err: return err
    rep = PoolReplica.query.get(replica_id)
    if rep and rep.node_id == g.node.id:
        f = rep.chunk.file
        db.session.delete(rep)
        db.session.flush()
        _maybe_finish_delete(f)
        db.session.commit()
    return jsonify({"status": "deleted"})


@pool_agent_bp.route("/agent/storage/chunks/<int:chunk_id>/upload", methods=["POST"])
@agent_auth()
def storage_chunk_upload(chunk_id):
    """A device sends back a chunk because someone is downloading the file."""
    err = _keyed_only()
    if err: return err
    rep = PoolReplica.query.filter_by(chunk_id=chunk_id, node_id=g.node.id, status="stored").first()
    if not rep:
        return jsonify({"error": "chunk not stored on this device"}), 403
    chunk = rep.chunk
    data = request.get_data()
    if hashlib.sha256(data).hexdigest() != chunk.sha256:
        return jsonify({"error": "chunk damaged (checksum mismatch)"}), 400
    if not chunk.tmp_blob_id:
        blob = PoolBlob(kind="chunk", name=f"chunk_{chunk.id}", size=len(data), data=data)
        db.session.add(blob)
        db.session.flush()
        chunk.tmp_blob_id = blob.id
        db.session.commit()
    return jsonify({"status": "received"})


@pool_agent_bp.route("/agent/storage/inventory", methods=["POST"])
@agent_auth()
def storage_inventory():
    """The device lists the chunk files it holds; anything not assigned to it can be removed."""
    err = _keyed_only()
    if err: return err
    held = [int(x) for x in (request.get_json(silent=True) or {}).get("chunk_ids", []) if str(x).isdigit()]
    if not held:
        return jsonify({"remove": []})
    assigned = {r.chunk_id for r in PoolReplica.query.filter(
        PoolReplica.node_id == g.node.id, PoolReplica.chunk_id.in_(held)).all()}
    return jsonify({"remove": [c for c in held if c not in assigned]})


# ---------------------------------------------------------------- helpers
def _maybe_finish_replication(f):
    """When a chunk has all wanted copies on contributors, drop the server's temporary copy.
    Until then the server keeps it, so the file is safe even with a single device online."""
    if f.status == "deleting":
        return
    done = True
    for c in f.chunks:
        stored = sum(1 for r in c.replicas if r.status == "stored")
        if stored < (f.replicas_wanted or 1):
            done = False
            continue
        if c.tmp_blob_id and f.download_requested_at is None:
            PoolBlob.query.filter_by(id=c.tmp_blob_id).delete()
            c.tmp_blob_id = None
    f.status = "stored" if done else "replicating"


def _maybe_finish_delete(f):
    left = (PoolReplica.query.join(PoolChunk, PoolChunk.id == PoolReplica.chunk_id)
            .filter(PoolChunk.file_id == f.id).count())
    if f.status == "deleting" and left == 0:
        for c in f.chunks:
            if c.tmp_blob_id:
                PoolBlob.query.filter_by(id=c.tmp_blob_id).delete()
        db.session.delete(f)
