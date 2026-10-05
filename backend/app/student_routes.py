from flask import Blueprint, request, jsonify
from datetime import datetime, timezone, timedelta
from app import db
from app.models import User, TaskRequest, MLResult
from app.allocation_engine import allocate_task, stop_task, log_event
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request

student_bp = Blueprint("student", __name__)

# =====================
# TASK MANAGEMENT (Student & Internal)
# =====================

@student_bp.route("/tasks/request", methods=["POST"])
@jwt_required()
def request_task_frontend():
    # Frontend route for task submission.
    # Added Guard Check to prevent multiple active tasks.
    
    data = request.get_json()
    user_id = data.get("user_id")

    # GUARD CHECK: Ensure User ID cannot have more than one non-terminal task
    active_task = TaskRequest.query.filter(
        TaskRequest.user_id == user_id,
        TaskRequest.status.in_(["pending", "starting", "running", "queued"])
    ).first()

    if active_task:
        return jsonify({
            "error": "You already have an active session.",
            "task_id": active_task.id,
            "status": active_task.status
        }), 400

    # CREATE NEW TASK
    task = TaskRequest(
        user_id=user_id,
        task_type=data["task_type"],
        mode=data["mode"],
        duration_minutes=data.get("duration_minutes"),
        status="pending",
        created_at=datetime.now(timezone.utc)
    )
    db.session.add(task)
    db.session.commit() 
    log_event(task.id, None, "pending", "Task created by student")
    db.session.commit()  # Commit the log entry
    
    # TRIGGER ALLOCATION
    result = allocate_task(task.id)
    return jsonify({"allocation": result, "task_id": task.id})

@student_bp.route("/tasks/<int:task_id>/allocate", methods=["POST"])
@jwt_required()
def manual_allocate(task_id):
    #Internal/Original allocation route - Kept for backward compatibility
    result = allocate_task(task_id)
    return jsonify(result)

@student_bp.route("/tasks/<int:task_id>/stop", methods=["POST"])
@jwt_required()
def manual_stop(task_id):
    #Internal/Original stop route - Kept for backward compatibility
    result = stop_task(task_id, reason="Manual Stop")
    return jsonify(result)

@student_bp.route("/tasks/my/<username>", methods=["GET"])
def get_my_sessions(username):
    #    Returns sessions and performs real-time cleanup.
    
    user = User.query.filter_by(username=username).first_or_404()
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

    # Remote-access passwords are only shown to the logged-in owner of the session
    owner = False
    try:
        verify_jwt_in_request(optional=True)
        owner = str(get_jwt_identity() or "") == str(user.id)
    except Exception:
        owner = False
    
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
