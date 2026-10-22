from flask import Blueprint, request, jsonify
from datetime import datetime, timezone, timedelta
from app import db
from app.models import User, TaskRequest, MLResult
from app.allocation_engine import allocate_task, default_priority, higher_or_equal_priority_pending, stop_task, log_event
from app.quotas import refresh_student_quota, session_quota_message
from app.guacamole_gateway import issue_ssh_connection_token, GuacamoleTokenError
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

student_bp = Blueprint("student", __name__)


def _current_user():
    """The user the JWT was issued to - never trust a client-supplied user id instead."""
    ident = str(get_jwt_identity())
    return User.query.get(int(ident)) if ident.isdigit() else User.query.filter_by(username=ident).first()


def _owns_task(task, user):
    role = (get_jwt() or {}).get("role")
    return bool(user) and (task.user_id == user.id or role == "admin")


# =====================
# TASK MANAGEMENT (Student & Internal)
# =====================

@student_bp.route("/tasks/request", methods=["POST"])
@jwt_required()
def request_task_frontend():
    # Frontend route for task submission.
    # Added Guard Check to prevent multiple active tasks.

    data = request.get_json()
    current_user = _current_user()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = current_user.id

    # GUARD CHECK: one source of truth for the session quota (see app/quotas.py) - this used
    # to check its own status list here, which omitted "allocated" and included a status that
    # was never actually used ("queued"), so a student with an allocated-but-not-yet-started
    # session could sneak a second one through.
    quota_error = session_quota_message(user_id)
    if quota_error:
        return jsonify({"error": quota_error}), 400

    # CREATE NEW TASK
    task = TaskRequest(
        user_id=user_id,
        task_type=data["task_type"],
        mode=data["mode"],
        duration_minutes=data.get("duration_minutes"),
        status="pending",
        priority=default_priority(data["mode"]),
        created_at=datetime.now(timezone.utc)
    )
    db.session.add(task)
    # Re-snapshot the ledger now that this task exists, in the same transaction the lock from
    # session_quota_message() above is still holding - otherwise the row would commit showing
    # the count from just before this request, not including it.
    refresh_student_quota(user_id)
    db.session.commit()
    log_event(task.id, None, "pending", "Task created by student")
    db.session.commit()  # Commit the log entry

    # TRIGGER ALLOCATION - but not ahead of a request that outranks (or ties) this one and is
    # already waiting; that one gets served first via the priority-ordered queue (app.tasks).
    if higher_or_equal_priority_pending(task.priority, exclude_id=task.id):
        result = {"status": "pending", "message": "Queued behind a higher-priority request."}
    else:
        result = allocate_task(task.id)
    return jsonify({"allocation": result, "task_id": task.id})

@student_bp.route("/tasks/<int:task_id>/allocate", methods=["POST"])
@jwt_required()
def manual_allocate(task_id):
    #Internal/Original allocation route - Kept for backward compatibility
    task = TaskRequest.query.get_or_404(task_id)
    if not _owns_task(task, _current_user()):
        return jsonify({"error": "Unauthorized"}), 403
    result = allocate_task(task_id)
    return jsonify(result)

@student_bp.route("/tasks/<int:task_id>/stop", methods=["POST"])
@jwt_required()
def manual_stop(task_id):
    #Internal/Original stop route - Kept for backward compatibility
    task = TaskRequest.query.get_or_404(task_id)
    if not _owns_task(task, _current_user()):
        return jsonify({"error": "Unauthorized"}), 403
    result = stop_task(task_id, reason="Manual Stop")
    return jsonify(result)

@student_bp.route("/tasks/<int:task_id>/guacamole-token", methods=["GET"])
@jwt_required()
def guacamole_token(task_id):
    """R4's remote-access gateway (see app.guacamole_gateway and
    docs/handoff/R4_sessions_gateway_plan.md item 2f): a short-lived Guacamole JSON-auth token
    for this one session's SSH connection, scoped to the requesting student and the session's
    own expiry - never issued for someone else's session. Ownership is re-checked inside
    issue_ssh_connection_token even though _owns_task already gates entry here, since that
    function is the one thing standing between "student asked for their own session's token"
    and "token granting access to session N" - it must never trust a task row without also
    checking whose it is."""
    task = TaskRequest.query.get_or_404(task_id)
    current_user = _current_user()
    if not _owns_task(task, current_user):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        token = issue_ssh_connection_token(task, current_user)
    except GuacamoleTokenError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"token": token})

@student_bp.route("/tasks/my/<username>", methods=["GET"])
@jwt_required()
def get_my_sessions(username):
    # Returns sessions and performs real-time cleanup. A student may only list their own
    # sessions; an admin may look up anyone's.
    user = User.query.filter_by(username=username).first_or_404()
    current_user = _current_user()
    role = (get_jwt() or {}).get("role")
    if not current_user or (current_user.id != user.id and role != "admin"):
        return jsonify({"error": "Unauthorized"}), 403
    now = datetime.now(timezone.utc)
    
    # Cleanup expired 'running' tasks
    expired = TaskRequest.query.filter(
        TaskRequest.user_id == user.id, 
        TaskRequest.status == "running", 
        TaskRequest.expiry_time.isnot(None), 
        TaskRequest.expiry_time < now
    ).all()
    for t in expired:
        stop_task(t.id, reason="Time Expired")

    # Cleanup 'starting' tasks stuck > 5 mins 
    stuck_limit = now - timedelta(minutes=5)
    stuck_tasks = TaskRequest.query.filter(
        TaskRequest.user_id == user.id,
        TaskRequest.status == "starting",
        TaskRequest.created_at < stuck_limit
    ).all()
    for t in stuck_tasks:
        t.status = "failed"
    
    if expired or stuck_tasks:
        db.session.commit()

    tasks = TaskRequest.query.filter_by(user_id=user.id).order_by(TaskRequest.created_at.desc()).all()

    # Remote-access passwords are only shown to the logged-in owner of the session (not even an admin)
    owner = current_user.id == user.id
    
    return jsonify([{
        "id": t.id,
        "task_type": t.task_type,
        "mode": t.mode,
        "duration_minutes": t.duration_minutes,
        "status": t.status,
        "assigned_pc": t.assigned_pc,
        "link": t.link,
        "start_time": t.start_time.isoformat() if t.start_time else None,
        "expiry_time": t.expiry_time.isoformat() if t.expiry_time else None,
        "vm_username": t.vm_username,
        "vm_password": t.vm_password if owner else None
    } for t in tasks])
    
@student_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
@jwt_required()
def student_delete_task(task_id):
    #Student removes their own task
    task = TaskRequest.query.get_or_404(task_id)
    
    # Ownership Check
    # The login token stores the user id (see auth_routes.create_access_token)
    ident = str(get_jwt_identity())
    current_user = User.query.get(int(ident)) if ident.isdigit() else User.query.filter_by(username=ident).first()
    if not current_user or task.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    
    # Safety: If active, stop the agent first
    if task.status in ["running", "starting"]:
        stop_task(task.id, reason="User deleted task from UI")

    if task.task_type == "ml_job_parent":
        # Find all child tasks associated with this parent
        TaskRequest.query.filter_by(parent_task_id=task.id).delete()
        MLResult.query.filter_by(task_id=task.id).delete()
    else:
        TaskRequest.query.filter_by(parent_task_id=task.id).delete()
        
    db.session.delete(task)
    db.session.commit()
    return jsonify({"message": "Session removed from your history"}), 200
