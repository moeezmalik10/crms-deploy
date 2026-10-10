import os
import secrets
from flask import Blueprint, request, jsonify
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash
from app import db
from app.models import (User, Node, NodeMetrics, NodeMetricsHistory, TaskRequest, MLResult,
                       TaskExecutionLog, DeviceEnrollment, PoolBlob, PoolChunk, PoolFile, PoolReplica)
from app.allocation_engine import stop_task
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from flask_jwt_extended import jwt_required, get_jwt

admin_bp = Blueprint("admin", __name__)

_SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
_SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()


@admin_bp.route("/admin/intrusion-logs", methods=["GET"])
@jwt_required()
def get_intrusion_logs():
    """Proxies intrusion_logs through the backend's own admin check, using the
    service_role key server-side - the browser's anon key is never given direct
    read access to this table (it holds attacker/victim emails and IPs)."""
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    if not (_SUPABASE_URL and _SUPABASE_KEY):
        return jsonify({"error": "Supabase is not configured on this server"}), 502

    import requests
    headers = {"apikey": _SUPABASE_KEY}
    if _SUPABASE_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {_SUPABASE_KEY}"
    try:
        r = requests.get(
            f"{_SUPABASE_URL}/rest/v1/intrusion_logs",
            headers=headers,
            params={"select": "*", "order": "created_at.desc", "limit": "500"},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        return jsonify({"error": f"could not read intrusion logs: {e}"}), 502
    return jsonify(r.json())

# =====================
# ADMIN – USERS
# =====================

@admin_bp.route("/admin/users", methods=["GET"])
@jwt_required()
def get_all_users():
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    users = User.query.all()
    return jsonify([{"id": u.id, "username": u.username, "role": u.role} for u in users])

@admin_bp.route("/admin/users", methods=["POST"])
@jwt_required()
def create_user():
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = data.get("role") or "student"
    if not email:
        return jsonify({"error": "email required"}), 400
    
    if not email.endswith("@uog.edu.pk"):
        return jsonify({"error": "Only @uog.edu.pk emails are allowed"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if role not in ("student", "admin"):
        return jsonify({"error": "Role must be student or admin"}), 400

    # The username is the part before @, so both must be unused
    username = email.split("@")[0]
    if User.query.filter(or_(db.func.lower(User.email) == email, User.username == username)).first():
        return jsonify({"error": f"A user with email {email} (username {username}) already exists"}), 409

    def insert():
        user = User(email=email, password=generate_password_hash(password), role=role)
        db.session.add(user)
        db.session.commit()
        return user

    try:
        user = insert()
    except IntegrityError as e:
        db.session.rollback()
        if "user_pkey" not in str(e.orig):
            return jsonify({"error": f"Could not create user: {e.orig}"}), 409
        # Rows were imported with fixed ids, so the id counter is behind. Move it past the
        # highest id and try once more.
        db.session.execute(text(
            "SELECT setval(pg_get_serial_sequence('\"user\"', 'id'), (SELECT COALESCE(MAX(id), 1) FROM \"user\"))"))
        db.session.commit()
        try:
            user = insert()
        except IntegrityError as e2:
            db.session.rollback()
            return jsonify({"error": f"Could not create user: {e2.orig}"}), 409
    return jsonify({"message": "User created", "id": user.id})

@admin_bp.route("/admin/users/<int:user_id>/reset_password", methods=["POST"])
@jwt_required()
def reset_password(user_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    # Password Reset functionality - a fresh random password every time, shown once to the
    # admin here, never a predictable fixed value every account would otherwise share.
    user = User.query.get_or_404(user_id)
    new_password = secrets.token_urlsafe(9)
    user.password = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"message": "Password reset", "new_password": new_password})

@admin_bp.route("/admin/users/<int:user_id>", methods=["DELETE"])
@jwt_required()
def delete_user(user_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403

    user = User.query.get_or_404(user_id)

    # Deleting a user used to throw a raw 500 (IntegrityError) the moment they had any
    # history at all - every foreign key pointing at them has to be resolved first. What to
    # do with each depends on what the data actually is, not just "delete everything":

    # Pool files still mid-replication/mid-delete hold real encrypted copies on contributors'
    # disks; deleting the row out from under that process would silently abandon those copies
    # instead of letting devices clean up properly. Ask each to finish deleting first (the
    # same path the student's own "Delete" button uses) and only proceed once none are left.
    still_cleaning_up = []
    for f in PoolFile.query.filter_by(owner_user_id=user.id).all():
        f.status = "deleting"
        for c in f.chunks:
            for r in c.replicas:
                if r.status == "pending":
                    db.session.delete(r)
                else:
                    r.status = "deleting"
        db.session.flush()
        left = (PoolReplica.query.join(PoolChunk, PoolChunk.id == PoolReplica.chunk_id)
                .filter(PoolChunk.file_id == f.id).count())
        if left == 0:
            for c in f.chunks:
                if c.tmp_blob_id:
                    PoolBlob.query.filter_by(id=c.tmp_blob_id).delete()
            db.session.delete(f)
        else:
            still_cleaning_up.append(f.name)
    if still_cleaning_up:
        db.session.rollback()
        return jsonify({"error": "Cannot delete yet - still cleaning up pool file(s): "
                                 f"{', '.join(still_cleaning_up)}. Try again in a minute."}), 409

    # This student's own task history - gone with them. A bulk delete() doesn't go through
    # the ORM, so it would just trade this 500 for another one at the next foreign key
    # (TaskExecutionLog.task_id is NOT NULL; MLResult.task_id has no cascade relationship
    # declared at all) - clear those first.
    task_ids = [t.id for t in TaskRequest.query.filter_by(user_id=user.id).all()]
    if task_ids:
        MLResult.query.filter(MLResult.task_id.in_(task_ids)).delete(synchronize_session=False)
        TaskExecutionLog.query.filter(TaskExecutionLog.task_id.in_(task_ids)).delete(synchronize_session=False)
        TaskRequest.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    # One-time join codes are meaningless without the student who made them.
    DeviceEnrollment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    # A contributed device is real hardware that keeps existing in the pool - orphan it
    # (ownerless) rather than deleting the Node row, consistent with how /agent/join already
    # treats an ownerless node.
    Node.query.filter_by(owner_user_id=user.id).update({"owner_user_id": None}, synchronize_session=False)

    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "User deleted"})

# =====================
# ADMIN – MONITORING
# =====================

@admin_bp.route("/admin/nodes", methods=["GET"])
@jwt_required()
def get_all_nodes():
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    # Returns all nodes with real-time health checks.
    # Standardized timezone awareness for health checks.
    nodes = Node.query.order_by(
        Node.status.desc(), # 'online' comes after 'offline' for better visibility
    ).all()
    results = []
    
    # Define the 'Ghost' Timeout 
    timeout_limit = datetime.now(timezone.utc) - timedelta(seconds=45)
    
    for n in nodes:
        latest = NodeMetrics.query.filter_by(node_id=n.id).order_by(NodeMetrics.timestamp.desc()).first()
        
        # Heartbeat comparison is timezone-aware
        heartbeat = n.last_heartbeat
        if heartbeat and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)

        is_active = n.status == "online" and heartbeat and heartbeat > timeout_limit
        display_status = "online" if is_active else "offline"

        # RAM and Task Calculations
        # All metrics reset to zero if the node is considered offline
        if latest and is_active:
            cpu_used = latest.cpu_used
            cpu_free = 100.0 - cpu_used
            ram_free = latest.memory_free_mb
            ram_used = n.total_ram_mb - ram_free
        else:
            # Default values for offline nodes
            cpu_used = 0.0
            cpu_free = 100.0
            ram_free = 0.0
            ram_used = 0.0
        
        active_tasks = TaskRequest.query.filter(
            TaskRequest.assigned_node_id == n.id,
            TaskRequest.status.in_(["running", "starting"])
        ).count()
        
        results.append({
            "id": n.id,
            "name": n.name,
            "ip": n.ip_address,
            "status": display_status,
            "cpu_used": cpu_used,
            "cpu_free": cpu_free,
            "ram_used": ram_used,
            "ram_free": ram_free,
            "total_ram": n.total_ram_mb,
            "running_tasks": active_tasks,
            "last_seen": heartbeat.isoformat() if heartbeat else None
        })
    
    return jsonify(results), 200

@admin_bp.route("/admin/nodes/<int:node_id>", methods=["DELETE"])
@jwt_required()
def delete_node(node_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    node = Node.query.get_or_404(node_id)

    # Check for active tasks before allowing node deletion - "allocated"/"starting" too, not
    # just "running": a task just assigned to this node (agent hasn't polled yet) would
    # otherwise have its assigned_node_id orphaned by the delete below.
    active_task = TaskRequest.query.filter(TaskRequest.assigned_node_id == node_id,
                                           TaskRequest.status.in_(("running", "starting", "allocated"))).first()
    if active_task:
        return jsonify({"error": "Cannot delete node while a task is running"}), 400

    # Every other foreign key pointing at this node has to be resolved too, or this throws a
    # raw 500 (IntegrityError) the moment the node has any history - it used to only check for
    # a *running* task and otherwise delete the row unconditionally.

    # Chunks this node was holding: just drop its replica rows. pool_storage_upkeep already
    # self-heals a missing replica (asks the surviving copy to resend, places a new one
    # elsewhere) - that is the normal "a device left the pool" path, reused here rather than
    # duplicated.
    PoolReplica.query.filter_by(node_id=node_id).delete(synchronize_session=False)
    # Historical tasks keep their assigned_pc (the device name, already a text snapshot) so
    # the history view still shows where they ran - just drop the dangling FK.
    TaskRequest.query.filter_by(assigned_node_id=node_id).update({"assigned_node_id": None}, synchronize_session=False)
    # Log messages already describe what happened in plain text; the FK is just bookkeeping.
    TaskExecutionLog.query.filter_by(node_id=node_id).update({"node_id": None}, synchronize_session=False)
    # A join code's "which node did this become" link is just bookkeeping too.
    DeviceEnrollment.query.filter_by(node_id=node_id).update({"node_id": None}, synchronize_session=False)
    # Metrics and their history have no meaning once the node is gone.
    NodeMetrics.query.filter_by(node_id=node_id).delete()
    NodeMetricsHistory.query.filter_by(node_id=node_id).delete()

    db.session.delete(node)
    db.session.commit()

    return jsonify({"message": f"Node {node.name} removed successfully"}), 200

def _last_reason(t):
    # Why a request failed or is waiting, so the admin can see it in History
    if t.status not in ("failed", "pending", "queued"):
        return ""
    last = TaskExecutionLog.query.filter_by(task_id=t.id).order_by(TaskExecutionLog.id.desc()).first()
    return (t.message if t.message and t.status == "failed" else (last.message if last else t.message or "")) or ""

@admin_bp.route("/admin-sessions", methods=["GET"])
@jwt_required()
def admin_sessions():
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    # Monitoring sessions with standardized dates and student names for better traceability.
    tasks = TaskRequest.query.order_by(TaskRequest.created_at.desc()).all()

    return jsonify({
        "sessions": [{
            "task_id": t.id,
            "student_id": t.user_id,
            "student_name": t.user.username if t.user else "Unknown",
            "task_type": t.task_type,
            "mode": t.mode,
            "machine": t.assigned_pc or "—",
            "start_time": t.start_time.isoformat() if t.start_time else "—",
            "duration": t.duration_minutes,
            "status": t.status,
            "reason": _last_reason(t)
        } for t in tasks]
    })

@admin_bp.route("/admin/tasks/<int:task_id>", methods=["DELETE"])
@jwt_required()
def admin_delete_task(task_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    #Admin removes task; logs the manual termination reason 
    task = TaskRequest.query.get_or_404(task_id)

    if task.status in ["running", "starting"]:
        stop_task(task.id, reason="Terminated manually by Admin") 
        
    if task.task_type == "ml_job_parent":
        # Find all child tasks associated with this parent
        TaskRequest.query.filter_by(parent_task_id=task.id).delete()
        MLResult.query.filter_by(task_id=task.id).delete()

    db.session.delete(task)
    db.session.commit()
    return jsonify({"message": f"Task {task_id} successfully deleted"}), 200
