import json
from flask import Blueprint, request, jsonify
from datetime import datetime, timezone, timedelta
from app import db
from app.models import Node, NodeMetrics, TaskRequest
from app.allocation_engine import stop_task, log_event, release_node_tasks 
from sqlalchemy import or_

agent_bp = Blueprint("agent", __name__)

# =====================
# AGENT ROUTES
# =====================
@agent_bp.route("/register_node", methods=["POST"])
def register_node():
    #Register or update Node hardware specs and IP address on heartbeat. 
    #This is the first step for any agent to be recognized by the system.
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400
    
    node_name = data.get("name")
    if not node_name:
        return jsonify({"error": "Node name is required"}), 400
    
    node = Node.query.filter_by(name=node_name).first()
    if not node:
        node = Node(name=node_name)
        db.session.add(node)
    
    node.total_cores = int(data.get("total_cores", 0))
    node.total_ram_mb = float(data.get("total_ram_mb", 0.0))
    node.ip_address = request.remote_addr
    node.status = "online"
    node.last_heartbeat = datetime.now(timezone.utc)
    
    db.session.commit()
    print(f"DATABASE CHECK: Node {node.name} updated.") 
    return jsonify({"node_id": node.id, "ram_in_db": node.total_ram_mb, "status": "registered"}), 200

@agent_bp.route("/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    # Standard heartbeat with metrics updates
    data = request.get_json()
    node = Node.query.filter_by(name=data["host"]["hostname"]).first()
    
    if not node:
        return jsonify({"error": "Node not registered"}), 404

    node.status = "online"
    node.last_heartbeat = datetime.now(timezone.utc)
    
    metrics = NodeMetrics.query.filter_by(node_id=node.id).first()
    if not metrics:
        metrics = NodeMetrics(node_id=node.id)
        db.session.add(metrics)

    metrics.cpu_used = data["host"]["cpu"]["used_percent"]
    metrics.memory_free_mb = data["host"]["memory"]["free"] / (1024*1024)
    metrics.timestamp = datetime.now(timezone.utc)
    metrics.raw_payload = data

    db.session.commit()
    return jsonify({"status": "updated"})

@agent_bp.route("/agent/leave", methods=["POST"])
def agent_leave():
    # A node is leaving the pool on purpose (browser page closed, "Leave the pool").
    # Sent with navigator.sendBeacon as text/plain, hence force=True.
    data = request.get_json(force=True, silent=True) or {}
    node = Node.query.filter_by(name=data.get("name")).first()
    if not node:
        return jsonify({"status": "unknown node"}), 404
    node.status = "offline"
    requeued, failed = release_node_tasks(node, "device left the pool")
    db.session.commit()
    return jsonify({"status": "offline", "requeued": requeued, "failed": failed})

# ==========================================
# POLLING & CALLBACK ROUTES (FOR AGENT)
# ==========================================
@agent_bp.route("/agent/tasks/poll/<pc_name>", methods=["GET"])
def poll_tasks(pc_name):
    # Unified Polling: Sends ML data if needed, otherwise standard VM data
    task = TaskRequest.query.filter(
        TaskRequest.assigned_pc == pc_name,
        TaskRequest.status == "allocated"
    ).order_by(TaskRequest.id.asc()).first()

    if not task:
        return jsonify({"command": "wait"}), 200

    # Mark as starting so other agents don't pick it up
    task.status = "starting"
    db.session.commit()
   
    if "ml" in task.task_type:
        # 1. Parse the Federated metadata from the database
        try:
            fed_data = json.loads(task.message) if task.message else {}
        except (json.JSONDecodeError, TypeError):
            fed_data = {}
        
        # 2. Return the command with Global Parameters
        return jsonify({
            "command": "start",
            "task_id": task.id,
            "task_type": "ml_task",
            "dataset_url": task.dataset_url,
            "model_type": task.model_type,
            "validation_type": task.validation_type,
            "chunk_id": task.chunk_id,
            "start_row": task.start_row,
            "end_row": task.end_row,
            # FEDERATED LEARNING PARAMS:
            "round": fed_data.get("round", 1),
            "global_weights": fed_data.get("weights"),   # None in Round 1
            "global_intercept": fed_data.get("intercept") # None in Round 1
        }), 200
    else:
        return jsonify({
            "command": "start",
            "task_id": task.id,
            "task_type": task.task_type,
            "mode": task.mode,
            "duration": task.duration_minutes
        }), 200

@agent_bp.route("/agent/tasks/<int:task_id>/ready", methods=["POST"])
def task_ready(task_id):
    #Transition task to running and set expiry (unlimited for ML)
    data = request.get_json()
    task = TaskRequest.query.get_or_404(task_id)
    
    task.status = "running"
    task.start_time = datetime.now(timezone.utc)
    
   # If duration exists, set expiry. If not (ML), it stays None (Unlimited).
    if task.duration_minutes:
        task.expiry_time = task.start_time + timedelta(minutes=task.duration_minutes)
    else:
        task.expiry_time = None
    
    if task.mode == "remote":
        task.link = data.get("link")
        task.vm_username = data.get("username")
        task.vm_password = data.get("password")
    
    log_event(task.id, task.assigned_node_id, "running", "Node Agent reported success. Session live.")
    db.session.commit()
    return jsonify({"status": "success"}), 200

@agent_bp.route("/agent/tasks/<int:task_id>/stop", methods=["POST"])
def agent_stop_task(task_id):
    result = stop_task(task_id, reason="Agent Process Finished") 
    return jsonify(result), 200
    
@agent_bp.route("/agent/tasks/<int:task_id>/error", methods=["POST"])
def task_error(task_id):
    data = request.get_json()
    task = TaskRequest.query.get_or_404(task_id)
    task.status = "failed"
    reason = data.get("reason", "Unknown agent error")
    log_event(task.id, task.assigned_node_id, "failed", f"Agent Error: {reason}")
    db.session.commit()
    return jsonify({"status": "noted", "error": reason}), 200
