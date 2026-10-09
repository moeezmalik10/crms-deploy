"""Website-side API of the resource pool.

Students contribute devices (join codes), see the pool, run sandboxed compute jobs and keep
files in pooled storage. Admins see every device with its IP addresses and can revoke it.
"""
import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone, timedelta

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required
from sqlalchemy import func

from app import db
from app.allocation_engine import (PRIORITY_BATCH, allocate_task, higher_or_equal_priority_pending,
                                   log_event, release_node_tasks, stop_task)
from app.models import (DeviceEnrollment, Node, NodeMetrics, NodeMetricsHistory, PoolBlob, PoolChunk,
                        PoolFile, PoolReplica, TaskExecutionLog, TaskRequest, User)
from app.pool_security import (decrypt_chunk, encrypt_chunk, new_file_key, new_join_code,
                               sha256_hex, unwrap_key, wrap_key)
from app.quotas import job_quota_message

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


@pool_bp.route("/pool/devices/<int:node_id>/history", methods=["GET"])
@jwt_required()
def device_history(node_id):
    """Up to 7 days of sampled CPU/RAM/storage history for this device, for a usage chart."""
    user, role = _me()
    n = Node.query.get_or_404(node_id)
    if not _can_manage(n, user, role):
        return jsonify({"error": "not your device"}), 403
    rows = (NodeMetricsHistory.query.filter_by(node_id=node_id)
            .order_by(NodeMetricsHistory.timestamp.asc()).all())
    return jsonify([{
        "timestamp": _aware(r.timestamp).isoformat(), "cpu_used": r.cpu_used,
        "memory_total_mb": r.memory_total_mb, "memory_free_mb": r.memory_free_mb,
        "storage_total_gb": r.storage_total_gb, "storage_free_gb": r.storage_free_gb,
    } for r in rows])


@pool_bp.route("/pool/ledger", methods=["GET"])
@jwt_required()
def ledger():
    """What the pool manager actually tracks per device: installed capacity, what is
    currently promised to a task that hasn't started yet ("reserved"), and what is actually
    running ("in_use") - the three-way split the proposal's pool manager is built around,
    instead of a single free/used number."""
    _, role = _me()
    if role != "admin":
        return jsonify({"error": "Admin access required"}), 403
    nodes = Node.query.filter(Node.device_key_hash.isnot(None)).all()
    node_ids = [n.id for n in nodes]
    active = TaskRequest.query.filter(
        TaskRequest.assigned_node_id.in_(node_ids),
        TaskRequest.status.in_(("allocated", "starting", "running")),
    ).all() if node_ids else []
    by_node = {n_id: {"reserved_cpu": 0, "reserved_ram_mb": 0, "in_use_cpu": 0, "in_use_ram_mb": 0} for n_id in node_ids}
    for t in active:
        bucket = "in_use" if t.status == "running" else "reserved"   # allocated/starting = promised, not started
        by_node[t.assigned_node_id][f"{bucket}_cpu"] += (t.required_cpu or 0)
        by_node[t.assigned_node_id][f"{bucket}_ram_mb"] += (t.required_ram_mb or 0)
    out = []
    for n in nodes:
        row = by_node[n.id]
        out.append({
            "id": n.id, "name": n.name, "state": node_state(n),
            "installed_cpu": n.total_cores, "installed_ram_mb": n.total_ram_mb,
            "shared_cpu": n.share_cores, "shared_ram_mb": n.share_ram_mb,
            **row,
            "free_cpu": max(0, (n.share_cores or n.total_cores or 0) - row["reserved_cpu"] - row["in_use_cpu"]),
            "free_ram_mb": max(0, (n.share_ram_mb or n.total_ram_mb or 0) - row["reserved_ram_mb"] - row["in_use_ram_mb"]),
        })
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


MAX_PARTS = 8


def _read_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


def _make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


def _split_lines(data, parts, header):
    """Split a text/CSV file into `parts` nearly equal row ranges; each part keeps the header."""
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    head, rows = (lines[:1], lines[1:]) if header and lines else ([], lines)
    out, n = [], len(rows)
    for i in range(parts):
        a, b = (n * i) // parts, (n * (i + 1)) // parts
        out.append("".join(head + rows[a:b]).encode("utf-8"))
    return out


@pool_bp.route("/pool/jobs", methods=["POST"])
@jwt_required()
def submit_job():
    """Submit a job. With parts > 1 the job is split across several devices that run at the
    same time; each part gets CRMS_PART / CRMS_PARTS and (optionally) its own slice of the data."""
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
            program = _read_zip(data)
        except zipfile.BadZipFile:
            return jsonify({"error": "the .zip file is damaged"}), 400
        if entry not in program:
            return jsonify({"error": f"{entry} is not inside the zip (found: {', '.join(list(program)[:8])})"}), 400
    else:
        entry = name
        program = {name: data}

    def num(key, lo, hi, default):
        try:
            return max(lo, min(hi, float(request.form.get(key, default))))
        except ValueError:
            return default

    kind = (request.form.get("kind") or "").strip().lower()
    if kind not in ("", "sweep"):
        return jsonify({"error": "kind must be empty (custom) or 'sweep'"}), 400

    param_sets = None
    if kind == "sweep":
        param_sets = [s for s in (request.form.get("param_sets") or "").splitlines() if s.strip()]
        if len(param_sets) < 2:
            return jsonify({"error": "a parameter sweep needs at least 2 parameter sets, one per line"}), 400
        if len(param_sets) > MAX_PARTS:
            return jsonify({"error": f"a parameter sweep supports at most {MAX_PARTS} parameter sets"}), 400
        parts = len(param_sets)   # one part per parameter set, not a separately chosen count
    else:
        parts = int(num("parts", 1, MAX_PARTS, 1))

    dfile = request.files.get("data")
    data_name, data_parts = None, None
    if dfile and dfile.filename:
        raw = dfile.read(MAX_JOB_INPUT + 1)
        if len(raw) > MAX_JOB_INPUT:
            return jsonify({"error": "data file is larger than 10 MB"}), 400
        data_name = os.path.basename(dfile.filename)
        header = request.form.get("header", "1") not in ("0", "false", "off")
        data_parts = _split_lines(raw, parts, header) if parts > 1 else [raw]

    quota_error = job_quota_message(user.id)
    if quota_error:
        return jsonify({"error": quota_error}), 400

    common = dict(user_id=user.id, task_type=f"job_{runtime}", job_runtime=runtime, job_entry=entry,
                  job_args=(request.form.get("args") or "")[:500], priority=PRIORITY_BATCH, job_kind=kind or None,
                  required_cpu=int(num("cores", 1, 16, 1)), required_ram_mb=num("ram_mb", 128, 32768, 1024),
                  required_disk_mb=num("disk_mb", 50, 20480, 500), duration_minutes=int(num("max_minutes", 1, 240, 10)))
    now = datetime.now(timezone.utc)

    def add_input(files, label):
        payload = _make_zip(files) if (len(files) > 1 or label.endswith(".zip")) else next(iter(files.values()))
        fname = label if len(files) == 1 and not label.endswith(".zip") else (label if label.endswith(".zip") else "job.zip")
        blob = PoolBlob(owner_user_id=user.id, kind="job_input", name=fname, size=len(payload), data=payload)
        db.session.add(blob)
        db.session.flush()
        return blob.id

    if parts == 1:
        files = dict(program)
        if data_parts:
            files[data_name] = data_parts[0]
        label = name if (len(files) == 1) else "job.zip"
        task = TaskRequest(mode="job", status="pending", input_blob_id=add_input(files, label), created_at=now, **common)
        db.session.add(task)
        db.session.flush()
        log_event(task.id, None, "pending", "Job submitted")
        db.session.commit()
        if higher_or_equal_priority_pending(task.priority, exclude_id=task.id):
            result = {"status": "pending", "message": "Queued behind a higher-priority request."}
        else:
            result = allocate_task(task.id)
        return jsonify({"job": _job_json(TaskRequest.query.get(task.id)), "allocation": result})

    # Split job: a group with one part per device
    group_message = (f"Parameter sweep over {parts} parameter sets" if kind == "sweep"
                     else f"Split into {parts} parts" + (f"; {data_name} split by rows" if data_name else ""))
    group = TaskRequest(mode="job_group", status="group", created_at=now, chunk_id=parts,
                        message=group_message, **common)
    db.session.add(group)
    db.session.flush()
    children = []
    for i in range(parts):
        files = dict(program)
        if data_parts:
            files[data_name] = data_parts[i]
        child_fields = dict(common)
        if param_sets:
            child_fields["job_args"] = param_sets[i][:500]
        child = TaskRequest(mode="job", status="pending", parent_task_id=group.id, chunk_id=i + 1,
                            input_blob_id=add_input(files, "job.zip"), created_at=now, **child_fields)
        db.session.add(child)
        db.session.flush()
        log_event(child.id, None, "pending", f"Part {i + 1} of {parts} submitted")
        children.append(child.id)
    db.session.commit()
    if higher_or_equal_priority_pending(PRIORITY_BATCH, exclude_ids=children):
        allocations = [{"status": "pending", "message": "Queued behind a higher-priority request."} for _ in children]
    else:
        allocations = [allocate_task(cid) for cid in children]
    return jsonify({"job": _group_json(TaskRequest.query.get(group.id)), "allocation": allocations})


def _sweep_scores(kids):
    """{chunk_id: score} for a parameter-sweep group's parts. Each part's program is expected
    to write output/result.json with a numeric "score" field - that file lands at the result
    zip's root (pool_runtime.run_job flattens the job's own output/ folder into the zip)."""
    scores = {}
    for c in kids:
        if c.status != "completed" or not c.result_blob_id:
            continue
        blob = PoolBlob.query.get(c.result_blob_id)
        if not blob:
            continue
        try:
            raw = _read_zip(blob.data).get("result.json")
            if raw is not None:
                score = json.loads(raw).get("score")
                if isinstance(score, (int, float)):
                    scores[c.chunk_id] = score
        except Exception:
            continue
    return scores


def _group_json(g):
    kids = sorted(g.sub_tasks, key=lambda c: c.chunk_id or 0)
    states = [c.status for c in kids]
    if any(s in ACTIVE + ("pending",) for s in states):
        status = "running" if any(s in ("running", "starting") for s in states) else (
            "allocated" if any(s == "allocated" for s in states) else "pending")
    else:
        status = "failed" if any(s == "failed" for s in states) else "completed"
    starts = [_aware(c.start_time) for c in kids if c.start_time]
    ends = [_aware(c.completed_at) for c in kids if c.completed_at]
    durations = [(_aware(c.completed_at) - _aware(c.start_time)).total_seconds()
                 for c in kids if c.start_time and c.completed_at]
    wall = (max(ends) - min(starts)).total_seconds() if starts and ends and status in ("completed", "failed") else None
    reason = ""
    for c in kids:
        if c.status in ("pending", "failed"):
            last = TaskExecutionLog.query.filter_by(task_id=c.id).order_by(TaskExecutionLog.id.desc()).first()
            reason = f"Part {c.chunk_id}: {last.message}" if last else ""
            break
    j = {
        "id": g.id, "group": True, "runtime": g.job_runtime, "entry": g.job_entry, "args": g.job_args,
        "cores": g.required_cpu, "ram_mb": g.required_ram_mb, "disk_mb": g.required_disk_mb,
        "max_minutes": g.duration_minutes, "status": status, "note": g.message,
        "devices": sorted({c.assigned_pc for c in kids if c.assigned_pc}),
        "created_at": _aware(g.created_at).isoformat() if g.created_at else None,
        "parts": [{"part": c.chunk_id, "status": c.status, "device": c.assigned_pc, "exit_code": c.exit_code,
                   "seconds": round((_aware(c.completed_at) - _aware(c.start_time)).total_seconds(), 1)
                   if c.start_time and c.completed_at else None} for c in kids],
        "parts_done": sum(1 for s in states if s in ("completed", "failed")),
        "wall_seconds": round(wall, 1) if wall is not None else None,
        "work_seconds": round(sum(durations), 1) if durations else None,
        "output": "\n".join(f"===== part {c.chunk_id} on {c.assigned_pc or '-'} =====\n{c.output_tail or ''}" for c in kids
                            if c.output_tail),
        "has_result": any(c.result_blob_id for c in kids), "reason": reason, "exit_code": None,
        "kind": g.job_kind,
    }
    if g.job_kind == "sweep" and status == "completed":
        scores = _sweep_scores(kids)
        if scores:
            best_chunk = max(scores, key=scores.get)
            j["best_part"] = best_chunk
            j["best_score"] = scores[best_chunk]
            j["best_args"] = next((c.job_args for c in kids if c.chunk_id == best_chunk), None)
    return j


@pool_bp.route("/pool/jobs", methods=["GET"])
@jwt_required()
def list_jobs():
    user, role = _me()
    q = TaskRequest.query.filter(
        ((TaskRequest.mode == "job") & TaskRequest.parent_task_id.is_(None)) | (TaskRequest.mode == "job_group"))
    if not (role == "admin" and request.args.get("all")):
        q = q.filter(TaskRequest.user_id == user.id)
    return jsonify([_group_json(t) if t.mode == "job_group" else _job_json(t)
                    for t in q.order_by(TaskRequest.id.desc()).limit(50).all()])


@pool_bp.route("/pool/jobs/<int:task_id>/result", methods=["GET"])
@jwt_required()
def job_result_download(task_id):
    user, role = _me()
    t = TaskRequest.query.get_or_404(task_id)
    if t.user_id != user.id and role != "admin":
        return jsonify({"error": "not your job"}), 403
    if t.mode == "job_group":
        return _group_result(t)
    blob = PoolBlob.query.get(t.result_blob_id) if t.result_blob_id else None
    if not blob:
        return jsonify({"error": "no result file"}), 404
    return Response(blob.data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{blob.name}"'})


def _group_result(g):
    """One zip for the whole split job: every part's files, plus CSV outputs merged back together."""
    kids = sorted(g.sub_tasks, key=lambda c: c.chunk_id or 0)
    files, csvs, summary = {}, {}, [f"Job #{g.id}: {g.message}", ""]
    for c in kids:
        secs = (_aware(c.completed_at) - _aware(c.start_time)).total_seconds() if c.start_time and c.completed_at else None
        summary.append(f"part {c.chunk_id}: {c.status} on {c.assigned_pc or '-'}"
                       + (f" in {secs:.1f} s" if secs is not None else "") + f" (exit code {c.exit_code})")
        blob = PoolBlob.query.get(c.result_blob_id) if c.result_blob_id else None
        if not blob:
            continue
        for n, b in _read_zip(blob.data).items():
            files[f"part_{c.chunk_id}/{n}"] = b
            if n.lower().endswith(".csv"):
                csvs.setdefault(n, []).append(b)
    for n, pieces in csvs.items():
        if len(pieces) == len(kids):   # same file from every part -> merge rows, keep one header
            first = pieces[0].decode("utf-8", errors="replace").splitlines(keepends=True)
            merged = first[:]
            for p in pieces[1:]:
                merged += p.decode("utf-8", errors="replace").splitlines(keepends=True)[1:]
            files[f"merged/{n}"] = "".join(merged).encode("utf-8")
    if g.job_kind == "sweep":
        scores = _sweep_scores(kids)
        if scores:
            best_chunk = max(scores, key=scores.get)
            best = next(c for c in kids if c.chunk_id == best_chunk)
            summary.append("")
            summary.append(f"Parameter sweep winner: part {best_chunk} (args: {best.job_args!r}), "
                           f"score {scores[best_chunk]}")
            if f"part_{best_chunk}/result.json" in files:
                files["best/result.json"] = files[f"part_{best_chunk}/result.json"]
        else:
            summary.append("")
            summary.append("Parameter sweep: no part reported a usable output/result.json score.")
    files["summary.txt"] = "\n".join(summary).encode("utf-8")
    return Response(_make_zip(files), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="job_{g.id}_all_parts.zip"'})


@pool_bp.route("/pool/jobs/<int:task_id>", methods=["DELETE"])
@jwt_required()
def delete_job(task_id):
    user, role = _me()
    t = TaskRequest.query.get_or_404(task_id)
    if t.user_id != user.id and role != "admin":
        return jsonify({"error": "not your job"}), 403
    if t.mode == "job_group":
        active = [c for c in t.sub_tasks if c.status in ACTIVE + ("pending",)]
        if active:
            for c in active:
                c.status = "failed"
                c.completed_at = datetime.now(timezone.utc)
                c.message = "Cancelled by the student"
                log_event(c.id, c.assigned_node_id, "failed", "Cancelled by the student")
            db.session.commit()
            return jsonify({"status": "cancelled"})
        for c in t.sub_tasks:
            for bid in (c.input_blob_id, c.result_blob_id):
                if bid:
                    PoolBlob.query.filter_by(id=bid).delete()
        db.session.delete(t)
        db.session.commit()
        return jsonify({"status": "deleted"})
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
    """Online contributed devices that share storage and have room, most room first. Locked
    for the rest of this transaction (same idea as allocate_task's node lock) so two uploads
    committed around the same time can't both believe the same device has room for them."""
    out = []
    for n in Node.query.filter(
        Node.device_key_hash.isnot(None), Node.paused.isnot(True)
    ).with_for_update(of=Node).all():
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
