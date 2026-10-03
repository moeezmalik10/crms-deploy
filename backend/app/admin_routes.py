from flask import Blueprint, request, jsonify
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash
from app import db
from app.models import User, Node, NodeMetrics, TaskRequest, MLResult
from app.allocation_engine import stop_task
from sqlalchemy import or_
from flask_jwt_extended import jwt_required, get_jwt

admin_bp = Blueprint("admin", __name__)

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
    
    data = request.get_json()
    email = data.get("email")
    if not email:
        return jsonify({"error": "email required"}), 400
    
    if "@uog.edu.pk" not in email:
        return jsonify({"error": "Only @uog.edu.pk emails are allowed"}), 400

    user = User(
        email=email,
        password=generate_password_hash(data.get("password", "123")),
        role=data.get("role", "student")
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({"message": "User created", "id": user.id})

@admin_bp.route("/admin/users/<int:user_id>/reset_password", methods=["POST"])
@jwt_required()
def reset_password(user_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    # Password Reset functionality
    user = User.query.get_or_404(user_id)
    user.password = generate_password_hash("123456")
    db.session.commit()
    return jsonify({"message": "Password reset to default 123456"})

@admin_bp.route("/admin/users/<int:user_id>", methods=["DELETE"])
@jwt_required()
def delete_user(user_id):
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    
    # User Deletion functionality
    user = User.query.get_or_404(user_id)
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
    
    # Check for active tasks before allowing node deletion
    active_task = TaskRequest.query.filter_by(assigned_node_id=node_id, status="running").first()
    if active_task:
        return jsonify({"error": "Cannot delete node while a task is running"}), 400

    #Delete associated metrics 
    NodeMetrics.query.filter_by(node_id=node_id).delete()
    
    db.session.delete(node)
    db.session.commit()
    
    return jsonify({"message": f"Node {node.name} removed successfully"}), 200

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
            "status": t.status
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
