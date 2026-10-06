"""Website-side API of the resource pool.

Students contribute devices (join codes), see the pool, run sandboxed compute jobs and keep
files in pooled storage. Admins see every device with its IP addresses and can revoke it.
"""
import hashlib
import io
import os
import zipfile
from datetime import datetime, timezone, timedelta

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required
from sqlalchemy import func

from app import db
from app.allocation_engine import allocate_task, log_event, release_node_tasks, stop_task
from app.models import (DeviceEnrollment, Node, NodeMetrics, PoolBlob, PoolChunk, PoolFile,
                        PoolReplica, TaskExecutionLog, TaskRequest, User)
from app.pool_security import (decrypt_chunk, encrypt_chunk, new_file_key, new_join_code,
                               sha256_hex, unwrap_key, wrap_key)

pool_bp = Blueprint("pool", __name__)

ONLINE_WINDOW = timedelta(seconds=60)
JOIN_CODE_MINUTES = 30
MAX_JOB_INPUT = 10 * 1024 * 1024
MAX_STORAGE_FILE = 25 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024
ACTIVE = ("allocated", "starting", "running")


# ---------------------------------------------------------------- helpers
def _me():
    ident = str(get_jwt_identity())
    user = User.query.get(int(ident)) if ident.isdigit() else User.query.filter_by(username=ident).first()
    return user, (get_jwt() or {}).get("role")


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def node_state(n):
    hb = _aware(n.last_heartbeat)
    alive = n.status == "online" and hb and hb > datetime.now(timezone.utc) - ONLINE_WINDOW
    if not alive:
        return "offline"
    return "paused" if n.paused else "online"


def _device_kind(n):
    if (n.name or "").upper().startswith("WEB-"):
        return "browser"
    return "contributed" if n.device_key_hash else "lab"


def _lent(node_id):
    """Resources currently lent out by a device (sessions, workspaces and jobs)."""
    rows = TaskRequest.query.filter(TaskRequest.assigned_node_id == node_id,
                                    TaskRequest.status.in_(ACTIVE)).all()
    return {
        "cores": sum(t.required_cpu or 0 for t in rows),
        "ram_mb": sum(t.required_ram_mb or 0 for t in rows),
        "disk_mb": sum(t.required_disk_mb or 0 for t in rows),
        "tasks": len(rows),
        "jobs": sum(1 for t in rows if t.mode == "job"),
        "workspaces": sum(1 for t in rows if t.mode == "remote"),
    }


def _storage_used_bytes(node_id):
    total = (db.session.query(func.coalesce(func.sum(PoolChunk.size), 0))
             .join(PoolReplica, PoolReplica.chunk_id == PoolChunk.id)
             .filter(PoolReplica.node_id == node_id).scalar())
    return int(total or 0)


def _device_json(n, show_private):
    m = NodeMetrics.query.filter_by(node_id=n.id).order_by(NodeMetrics.timestamp.desc()).first()
    state = node_state(n)
    live = state != "offline"
    owner = User.query.get(n.owner_user_id) if n.owner_user_id else None
    lent = _lent(n.id)
    d = {
        "id": n.id,
        "name": n.name,
        "kind": _device_kind(n),
        "verified": bool(n.device_key_hash),
        "owner": owner.username if owner else None,
        "state": state,
        "last_seen": _aware(n.last_heartbeat).isoformat() if n.last_heartbeat else None,
        "os": n.os_name,
        "cpu_model": n.cpu_model,
        "cores": n.total_cores or 0,
        "cpu_ghz": n.cpu_ghz,
        "cpu_used": round(m.cpu_used, 1) if (m and live and m.cpu_used is not None) else None,
        "ram_total_mb": round(n.total_ram_mb or 0),
        "ram_free_mb": round(m.memory_free_mb) if (m and live and m.memory_free_mb is not None) else None,
        "storage_total_gb": round(n.total_storage_gb, 1) if n.total_storage_gb else None,
        "storage_free_gb": round(n.free_storage_gb, 1) if n.free_storage_gb else None,
        "share_cores": n.share_cores,
        "share_ram_mb": n.share_ram_mb,
        "share_storage_gb": n.share_storage_gb,
        "allow_light_sandbox": bool(n.allow_light_sandbox),
        "sandbox_mode": n.sandbox_mode,
        "agent_version": n.agent_version,
        "lent": lent,
        "pool_storage_used_mb": round(_storage_used_bytes(n.id) / 1024**2, 1),
    }
    if show_private:
        d["lan_ip"] = n.lan_ip
        d["public_ip"] = n.public_ip
    return d


def _can_manage(n, user, role):
    return role == "admin" or (user and n.owner_user_id == user.id)


# ---------------------------------------------------------------- devices
@pool_bp.route("/pool/enroll", methods=["POST"])
@jwt_required()
def enroll():
    """Make a one-time join code. The agent turns it into a permanent device key."""
    user, _ = _me()
    if not user:
        return jsonify({"error": "user not found"}), 404
    d = request.get_json(silent=True) or {}

    def num(key, lo, hi):
        v = d.get(key)
        if v in (None, ""):
            return None
        return max(lo, min(hi, float(v)))

    code = new_join_code()
    enr = DeviceEnrollment(
        user_id=user.id, code_hash=sha256_hex(code),
        share_cores=num("share_cores", 0.5, 128), share_ram_mb=num("share_ram_mb", 256, 1048576),
        share_storage_gb=num("share_storage_gb", 0, 10000),
        allow_light_sandbox=bool(d.get("allow_light_sandbox")),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=JOIN_CODE_MINUTES))
    db.session.add(enr)
    db.session.commit()
    server = request.host_url.rstrip("/")
    return jsonify({"code": code, "server": server, "expires_at": enr.expires_at.isoformat(),
                    "valid_minutes": JOIN_CODE_MINUTES})


@pool_bp.route("/pool/devices", methods=["GET"])
@jwt_required()
def devices():
    user, role = _me()
    q = Node.query
    # Students see the whole pool without IP addresses ("everyone"), or their own devices
    everyone = role != "admin" and request.args.get("everyone")
    if (role != "admin" and not everyone) or request.args.get("mine"):
        # "My devices": the ones this student contributed and that are still in the pool
        q = q.filter(Node.owner_user_id == (user.id if user else -1), Node.device_key_hash.isnot(None))
    nodes = q.order_by(Node.name).all()
    out = [_device_json(n, show_private=(role == "admin" or (user and n.owner_user_id == user.id)))
           for n in nodes]
    order = {"online": 0, "paused": 1, "offline": 2}
    out.sort(key=lambda d: (order[d["state"]], d["name"]))
    return jsonify(out)


@pool_bp.route("/pool/devices/<int:node_id>", methods=["PATCH"])
@jwt_required()
def update_device(node_id):
    user, role = _me()
    n = Node.query.get_or_404(node_id)
    if not _can_manage(n, user, role):
        return jsonify({"error": "not your device"}), 403
    d = request.get_json(silent=True) or {}
    for key, lo, hi in (("share_cores", 0.5, 128), ("share_ram_mb", 256, 1048576), ("share_storage_gb", 0, 10000)):
        if key in d:
            setattr(n, key, None if d[key] in (None, "") else max(lo, min(hi, float(d[key]))))
    if "paused" in d:
        n.paused = bool(d["paused"])
    if "allow_light_sandbox" in d:
        n.allow_light_sandbox = bool(d["allow_light_sandbox"])
    db.session.commit()
    return jsonify(_device_json(n, show_private=True))


@pool_bp.route("/pool/devices/<int:node_id>", methods=["DELETE"])
@jwt_required()
def revoke_device(node_id):
    """Remove a device from the pool: its key stops working at once."""
    user, role = _me()
    n = Node.query.get_or_404(node_id)
    if not _can_manage(n, user, role):
        return jsonify({"error": "not your device"}), 403
    n.device_key_hash = None
    n.status = "offline"
    n.paused = True
    requeued, failed = release_node_tasks(n, "device removed from the pool")
    # Its storage copies are gone; files keep their other copies and get new ones.
    lost = PoolReplica.query.filter_by(node_id=n.id).all()
    for r in lost:
        db.session.delete(r)
    db.session.commit()
    return jsonify({"status": "removed", "requeued": requeued, "failed": failed, "storage_copies_lost": len(lost)})


@pool_bp.route("/pool/overview", methods=["GET"])
@jwt_required()
def overview():
    nodes = Node.query.all()
    t = {k: 0 for k in ("devices", "online", "offline", "paused", "cores_online", "cores_all",
                        "ghz_total", "ram_total_mb", "ram_free_mb", "storage_total_gb", "storage_free_gb",
                        "shared_cores", "shared_ram_mb", "shared_storage_gb",
                        "lent_cores", "lent_ram_mb", "lent_disk_mb", "jobs_running", "workspaces_running")}
    cpu_used = []
    for n in nodes:
        state = node_state(n)
        t["devices"] += 1
        t[state] += 1
        t["cores_all"] += n.total_cores or 0
        if state == "offline":
            continue
        m = NodeMetrics.query.filter_by(node_id=n.id).order_by(NodeMetrics.timestamp.desc()).first()
        cores = n.total_cores or 0
        t["cores_online"] += cores
        if n.cpu_ghz:
            t["ghz_total"] += cores * n.cpu_ghz
        t["ram_total_mb"] += n.total_ram_mb or 0
        if m and m.memory_free_mb is not None:
            t["ram_free_mb"] += m.memory_free_mb
        if m and m.cpu_used is not None:
            cpu_used.append(m.cpu_used)
        t["storage_total_gb"] += n.total_storage_gb or 0
        t["storage_free_gb"] += n.free_storage_gb or 0
        lent = _lent(n.id)
        t["lent_cores"] += lent["cores"]
        t["lent_ram_mb"] += lent["ram_mb"]
        t["lent_disk_mb"] += lent["disk_mb"]
        t["jobs_running"] += lent["jobs"]
        t["workspaces_running"] += lent["workspaces"]
        if state == "online":
            t["shared_cores"] += min(cores, n.share_cores) if n.share_cores else cores
            t["shared_ram_mb"] += min(n.total_ram_mb or 0, n.share_ram_mb) if n.share_ram_mb else (n.total_ram_mb or 0)
            t["shared_storage_gb"] += n.share_storage_gb or 0
    t["cpu_used_avg"] = round(sum(cpu_used) / len(cpu_used), 1) if cpu_used else 0
    used = db.session.query(func.coalesce(func.sum(PoolChunk.size), 0)).join(
        PoolReplica, PoolReplica.chunk_id == PoolChunk.id).scalar()
    t["pool_storage_used_gb"] = round(int(used or 0) / 1024**3, 3)
    t["pool_files"] = PoolFile.query.filter(PoolFile.status != "deleting").count()
    for k in ("ghz_total", "ram_total_mb", "ram_free_mb", "storage_total_gb", "storage_free_gb",
              "shared_cores", "shared_ram_mb", "shared_storage_gb", "lent_ram_mb", "lent_disk_mb"):
        t[k] = round(t[k], 1)
    return jsonify(t)


# ---------------------------------------------------------------- compute jobs
def _job_json(t):
    last = TaskExecutionLog.query.filter_by(task_id=t.id).order_by(TaskExecutionLog.id.desc()).first()
    return {
        "id": t.id, "runtime": t.job_runtime, "entry": t.job_entry, "args": t.job_args,
        "cores": t.required_cpu, "ram_mb": t.required_ram_mb, "disk_mb": t.required_disk_mb,
        "max_minutes": t.duration_minutes, "status": t.status, "device": t.assigned_pc,
        "created_at": _aware(t.created_at).isoformat() if t.created_at else None,
        "start_time": _aware(t.start_time).isoformat() if t.start_time else None,
        "completed_at": _aware(t.completed_at).isoformat() if t.completed_at else None,
        "exit_code": t.exit_code, "output": t.output_tail or "",
        "has_result": bool(t.result_blob_id), "reason": last.message if last else "",
    }


@pool_bp.route("/pool/jobs", methods=["POST"])
@jwt_required()
def submit_job():
    user, _ = _me()
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "choose a .py, .cpp or .zip file"}), 400
    runtime = request.form.get("runtime", "python")
    if runtime not in ("python", "cpp"):
        return jsonify({"error": "runtime must be python or cpp"}), 400
    data = f.read(MAX_JOB_INPUT + 1)
    if len(data) > MAX_JOB_INPUT:
        return jsonify({"error": "file is larger than 10 MB"}), 400
    name = os.path.basename(f.filename or "input")
    entry = (request.form.get("entry") or "").strip() or ("main.py" if runtime == "python" else "main.cpp")
    if name.lower().endswith(".zip"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return jsonify({"error": "the .zip file is damaged"}), 400
        if entry not in names:
            return jsonify({"error": f"{entry} is not inside the zip (found: {', '.join(names[:8])})"}), 400
    else:
        entry = name
    active = TaskRequest.query.filter(TaskRequest.user_id == user.id, TaskRequest.mode == "job",
                                      TaskRequest.status.in_(("pending",) + ACTIVE)).count()
    if active >= 3:
        return jsonify({"error": "you already have 3 jobs waiting or running"}), 400

    def num(key, lo, hi, default):
        try:
            return max(lo, min(hi, float(request.form.get(key, default))))
        except ValueError:
            return default

    blob = PoolBlob(owner_user_id=user.id, kind="job_input", name=name, size=len(data), data=data)
    db.session.add(blob)
    db.session.flush()
    task = TaskRequest(
        user_id=user.id, task_type=f"job_{runtime}", mode="job", status="pending",
        job_runtime=runtime, job_entry=entry, job_args=(request.form.get("args") or "")[:500],
        required_cpu=int(num("cores", 1, 16, 1)), required_ram_mb=num("ram_mb", 128, 32768, 1024),
        required_disk_mb=num("disk_mb", 50, 20480, 500), duration_minutes=int(num("max_minutes", 1, 240, 10)),
        input_blob_id=blob.id, created_at=datetime.now(timezone.utc))
    db.session.add(task)
    db.session.flush()
    log_event(task.id, None, "pending", "Job submitted")
    db.session.commit()
    result = allocate_task(task.id)
    return jsonify({"job": _job_json(TaskRequest.query.get(task.id)), "allocation": result})


@pool_bp.route("/pool/jobs", methods=["GET"])
@jwt_required()
def list_jobs():
    user, role = _me()
    q = TaskRequest.query.filter(TaskRequest.mode == "job")
    if not (role == "admin" and request.args.get("all")):
        q = q.filter(TaskRequest.user_id == user.id)
    return jsonify([_job_json(t) for t in q.order_by(TaskRequest.id.desc()).limit(50).all()])


@pool_bp.route("/pool/jobs/<int:task_id>/result", methods=["GET"])
@jwt_required()
def job_result_download(task_id):
    user, role = _me()
    t = TaskRequest.query.get_or_404(task_id)
    if t.user_id != user.id and role != "admin":
        return jsonify({"error": "not your job"}), 403
    blob = PoolBlob.query.get(t.result_blob_id) if t.result_blob_id else None
    if not blob:
        return jsonify({"error": "no result file"}), 404
    return Response(blob.data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{blob.name}"'})


@pool_bp.route("/pool/jobs/<int:task_id>", methods=["DELETE"])
@jwt_required()
def delete_job(task_id):
    user, role = _me()
    t = TaskRequest.query.get_or_404(task_id)
    if t.user_id != user.id and role != "admin":
        return jsonify({"error": "not your job"}), 403
    if t.status in ACTIVE or t.status == "pending":
        # The agent sees the new status within seconds and removes the sandbox
        t.status = "failed"
        t.completed_at = datetime.now(timezone.utc)
        t.message = "Cancelled by the student"
        log_event(t.id, t.assigned_node_id, "failed", "Cancelled by the student")
        db.session.commit()
        return jsonify({"status": "cancelled"})
    for bid in (t.input_blob_id, t.result_blob_id):
        if bid:
            PoolBlob.query.filter_by(id=bid).delete()
    db.session.delete(t)
    db.session.commit()
    return jsonify({"status": "deleted"})


# ---------------------------------------------------------------- pooled storage
def _storage_targets(size, exclude=()):
    """Online contributed devices that share storage and have room, most room first."""
    out = []
    for n in Node.query.filter(Node.device_key_hash.isnot(None), Node.paused.isnot(True)).all():
        if n.id in exclude or node_state(n) != "online" or not n.share_storage_gb:
            continue
        room = n.share_storage_gb * 1024**3 - _storage_used_bytes(n.id)
        if n.free_storage_gb is not None:
            room = min(room, (n.free_storage_gb - 2) * 1024**3)   # never fill the owner's disk
        if room >= size:
            out.append((room, n))
    out.sort(key=lambda x: -x[0])
    return [n for _, n in out]


def _file_json(f):
    chunks = f.chunks
    online_ids = {n.id for n in Node.query.all() if node_state(n) == "online"}
    copies, available, ready = [], True, 0
    for c in chunks:
        stored = [r for r in c.replicas if r.status == "stored"]
        copies.append(len(stored))
        if c.tmp_blob_id:
            ready += 1
        elif not any(r.node_id in online_ids for r in stored):
            available = False
    holders = sorted({Node.query.get(r.node_id).name for c in chunks for r in c.replicas if r.status == "stored"})
    return {
        "id": f.id, "name": f.name, "size": f.size, "status": f.status,
        "created_at": _aware(f.created_at).isoformat() if f.created_at else None,
        "copies": min(copies) if copies else 0, "copies_wanted": f.replicas_wanted,
        "devices": holders, "available": available,
        "chunks": len(chunks), "chunks_ready": ready,
        "download_ready": bool(chunks) and ready == len(chunks),
        "server_backup": any(c.tmp_blob_id for c in chunks) and f.download_requested_at is None,
    }


@pool_bp.route("/pool/storage", methods=["POST"])
@jwt_required()
def storage_upload():
    user, _ = _me()
    up = request.files.get("file")
    if not up:
        return jsonify({"error": "choose a file"}), 400
    data = up.read(MAX_STORAGE_FILE + 1)
    if len(data) > MAX_STORAGE_FILE:
        return jsonify({"error": "files up to 25 MB can be stored in the pool"}), 400
    enc_estimate = len(data) + 28 * (len(data) // CHUNK_SIZE + 1)
    targets = _storage_targets(enc_estimate)
    if not targets:
        return jsonify({"error": "no online device is sharing enough storage right now"}), 409
    key = new_file_key()
    f = PoolFile(owner_user_id=user.id, name=os.path.basename(up.filename or "file")[:255], size=len(data),
                 sha256=hashlib.sha256(data).hexdigest(), wrapped_key=wrap_key(key),
                 replicas_wanted=2, status="replicating")
    db.session.add(f)
    db.session.flush()
    pieces = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)] or [b""]
    f.chunk_count = len(pieces)
    for idx, piece in enumerate(pieces):
        enc = encrypt_chunk(key, piece, f"{f.id}:{idx}".encode())
        blob = PoolBlob(kind="chunk", name=f"file{f.id}_chunk{idx}", size=len(enc), data=enc)
        db.session.add(blob)
        db.session.flush()
        c = PoolChunk(file_id=f.id, idx=idx, size=len(enc), sha256=hashlib.sha256(enc).hexdigest(),
                      tmp_blob_id=blob.id)
        db.session.add(c)
        db.session.flush()
        for n in targets[:2]:
            db.session.add(PoolReplica(chunk_id=c.id, node_id=n.id, status="pending"))
    db.session.commit()
    return jsonify(_file_json(f))


@pool_bp.route("/pool/storage", methods=["GET"])
@jwt_required()
def storage_list():
    user, role = _me()
    q = PoolFile.query.filter(PoolFile.status != "deleting")
    if not (role == "admin" and request.args.get("all")):
        q = q.filter(PoolFile.owner_user_id == user.id)
    return jsonify([_file_json(f) for f in q.order_by(PoolFile.id.desc()).all()])


def _own_file(file_id):
    user, role = _me()
    f = PoolFile.query.get_or_404(file_id)
    if f.owner_user_id != user.id and role != "admin":
        return None
    return f


@pool_bp.route("/pool/storage/<int:file_id>/prepare", methods=["POST"])
@jwt_required()
def storage_prepare(file_id):
    """Ask the devices that hold the chunks to send them back for a download."""
    f = _own_file(file_id)
    if not f:
        return jsonify({"error": "not your file"}), 403
    f.download_requested_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(_file_json(f))


@pool_bp.route("/pool/storage/<int:file_id>/download", methods=["GET"])
@jwt_required()
def storage_download(file_id):
    f = _own_file(file_id)
    if not f:
        return jsonify({"error": "not your file"}), 403
    chunks = sorted(f.chunks, key=lambda c: c.idx)
    if any(not c.tmp_blob_id for c in chunks):
        return jsonify({"error": "not ready yet - press Prepare and wait until all parts arrived"}), 409
    key = unwrap_key(f.wrapped_key)
    out = io.BytesIO()
    for c in chunks:
        blob = PoolBlob.query.get(c.tmp_blob_id)
        out.write(decrypt_chunk(key, blob.data, f"{f.id}:{c.idx}".encode()))
    data = out.getvalue()
    if hashlib.sha256(data).hexdigest() != f.sha256:
        return jsonify({"error": "file check failed"}), 500
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{f.name}"'})


@pool_bp.route("/pool/storage/<int:file_id>", methods=["DELETE"])
@jwt_required()
def storage_delete(file_id):
    f = _own_file(file_id)
    if not f:
        return jsonify({"error": "not your file"}), 403
    f.status = "deleting"
    has_copies = False
    for c in f.chunks:
        for r in c.replicas:
            if r.status == "pending":
                db.session.delete(r)          # never reached the device
            else:
                r.status = "deleting"         # the device removes its copy, then confirms
                has_copies = True
        if c.tmp_blob_id:
            PoolBlob.query.filter_by(id=c.tmp_blob_id).delete()
            c.tmp_blob_id = None
    if not has_copies:
        db.session.delete(f)
    db.session.commit()
    return jsonify({"status": "deleting" if has_copies else "deleted"})
