import json
from flask import Blueprint, request, jsonify, g
from datetime import datetime, timezone, timedelta
from app import db
from app.models import Node, NodeMetrics, TaskRequest
from app.pool_security import agent_auth, task_belongs_to_caller, client_public_ip
from app.allocation_engine import stop_task, log_event, release_node_tasks 
from sqlalchemy import or_

agent_bp = Blueprint("agent", __name__)

# =====================
# AGENT ROUTES
# =====================
def _apply_hardware(node, data):
    """Hardware facts an agent reports at registration and in heartbeats."""
    hw = data.get("hardware") or {}
    if hw.get("lan_ip"): node.lan_ip = str(hw["lan_ip"])[:64]
    if hw.get("os"): node.os_name = str(hw["os"])[:120]
    if hw.get("cpu_model"): node.cpu_model = str(hw["cpu_model"])[:160]
    if hw.get("cpu_ghz"): node.cpu_ghz = float(hw["cpu_ghz"])
    if hw.get("storage_total_gb") is not None: node.total_storage_gb = float(hw["storage_total_gb"])
    if hw.get("storage_free_gb") is not None: node.free_storage_gb = float(hw["storage_free_gb"])
    if hw.get("sandbox_mode"): node.sandbox_mode = str(hw["sandbox_mode"])[:20]
    if hw.get("agent_version"): node.agent_version = str(hw["agent_version"])[:20]
    node.public_ip = client_public_ip()[:64]
    node.ip_address = node.lan_ip or node.public_ip


def _settings(node):
    """What the owner chose on the website; the agent follows it."""
    return {
        "node_name": node.name,
        "paused": bool(node.paused),
        "share_cores": node.share_cores,
        "share_ram_mb": node.share_ram_mb,
        "share_storage_gb": node.share_storage_gb,
        "allow_light_sandbox": bool(node.allow_light_sandbox),
    }


def _parts_of(task):
    if not task.parent_task_id:
        return 1
    parent = db.session.get(TaskRequest, task.parent_task_id)
    return (parent.chunk_id if parent and parent.chunk_id else 1)


@agent_bp.route("/register_node", methods=["POST"])
@agent_auth()
def register_node():
    #Register or update Node hardware specs and IP address on heartbeat. 
    #This is the first step for any agent to be recognized by the system.
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400
    
    if g.node is not None:
        node = g.node                      # keyed device: identity comes from its key
    else:
        node_name = data.get("name")
        if not node_name:
            return jsonify({"error": "Node name is required"}), 400
        node = Node.query.filter_by(name=node_name).first()
        if node and node.device_key_hash:
            return jsonify({"error": "this device is protected by a device key"}), 401
        if not node:
            node = Node(name=node_name)
            db.session.add(node)
    
    node.total_cores = int(data.get("total_cores", 0))
    node.total_ram_mb = float(data.get("total_ram_mb", 0.0))
    _apply_hardware(node, data)
    node.status = "online"
    node.last_heartbeat = datetime.now(timezone.utc)
    
    db.session.commit()
    print(f"DATABASE CHECK: Node {node.name} updated.") 
    return jsonify({"node_id": node.id, "ram_in_db": node.total_ram_mb, "status": "registered",
                    "settings": _settings(node)}), 200

@agent_bp.route("/agent/heartbeat", methods=["POST"])
@agent_auth()
def agent_heartbeat():
    # Standard heartbeat with metrics updates
    data = request.get_json()
    if g.node is not None:
        node = g.node
    else:
        node = Node.query.filter_by(name=data["host"]["hostname"]).first()
        if node and node.device_key_hash:
            return jsonify({"error": "this device is protected by a device key"}), 401
    
    if not node:
        return jsonify({"error": "Node not registered"}), 404
    _apply_hardware(node, data)

    node.status = "online"
    node.last_heartbeat = datetime.now(timezone.utc)
    
    metrics = NodeMetrics.query.filter_by(node_id=node.id).first()
    if not metrics:
        metrics = NodeMetrics(node_id=node.id)
        db.session.add(metrics)

    metrics.cpu_used = data["host"]["cpu"]["used_percent"]
    metrics.memory_free_mb = data["host"]["memory"]["free"] / (1024*1024)
    mem_total = data["host"]["memory"].get("total")
    if mem_total:
        metrics.memory_total_mb = mem_total / (1024*1024)
        metrics.memory_used_mb = metrics.memory_total_mb - metrics.memory_free_mb
    st = data["host"].get("storage") or {}
    if st.get("total"):
        metrics.storage_total_gb = st["total"] / 1024**3
        metrics.storage_free_gb = st.get("free", 0) / 1024**3
        metrics.storage_used_gb = metrics.storage_total_gb - metrics.storage_free_gb
        node.total_storage_gb = metrics.storage_total_gb
        node.free_storage_gb = metrics.storage_free_gb
    metrics.cpu_free = 100.0 - (metrics.cpu_used or 0)
    metrics.timestamp = datetime.now(timezone.utc)
    metrics.raw_payload = data

    db.session.commit()
    return jsonify({"status": "updated", "settings": _settings(node)})

@agent_bp.route("/agent/leave", methods=["POST"])
@agent_auth()
def agent_leave():
    # A node is leaving the pool on purpose (browser page closed, "Leave the pool").
    # Sent with navigator.sendBeacon as text/plain, hence force=True.
    data = request.get_json(force=True, silent=True) or {}
    node = g.node or Node.query.filter_by(name=data.get("name")).first()
    if not node:
        return jsonify({"status": "unknown node"}), 404
    if g.node is None and node.device_key_hash:
        return jsonify({"error": "this device is protected by a device key"}), 401
    node.status = "offline"
    requeued, failed = release_node_tasks(node, "device left the pool")
    db.session.commit()
    return jsonify({"status": "offline", "requeued": requeued, "failed": failed})

# ==========================================
# POLLING & CALLBACK ROUTES (FOR AGENT)
# ==========================================
@agent_bp.route("/agent/tasks/poll/<pc_name>", methods=["GET"])
@agent_auth("pc_name")
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

    if task.mode == "job":
        return jsonify({
            "command": "start",
            "task_id": task.id,
            "task_type": task.task_type,
            "mode": "job",
            "runtime": task.job_runtime,
            "entry": task.job_entry,
            "args": task.job_args or "",
            "cores": task.required_cpu or 1,
            "ram_mb": task.required_ram_mb or 1024,
            "disk_mb": task.required_disk_mb or 500,
            "max_minutes": task.duration_minutes or 10,
            "input_path": f"/agent/jobs/{task.id}/input",
            # For a job split across devices: which part this is and how many there are
            "part": (task.chunk_id or 1) if task.parent_task_id else 1,
            "parts": _parts_of(task),
        }), 200
   
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
            "duration": task.duration_minutes,
            "cores": task.required_cpu or 1,
            "ram_mb": task.required_ram_mb or 2048,
            "disk_mb": task.required_disk_mb or 3072,
        }), 200

@agent_bp.route("/agent/tasks/<int:task_id>/ready", methods=["POST"])
@agent_auth()
def task_ready(task_id):
    #Transition task to running and set expiry (unlimited for ML)
    data = request.get_json()
    task = TaskRequest.query.get_or_404(task_id)
    if not task_belongs_to_caller(task):
        return jsonify({"error": "not your task"}), 403
    
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

@agent_bp.route("/agent/tasks/<int:task_id>/status", methods=["GET"])
@agent_auth()
def agent_task_status(task_id):
    # Lets an agent close remote access as soon as a session is stopped or deleted on the website
    task = TaskRequest.query.get(task_id)
    if not task:
        return jsonify({"status": "deleted"}), 404
    if not task_belongs_to_caller(task):
        return jsonify({"error": "not your task"}), 403
    return jsonify({"status": task.status}), 200

@agent_bp.route("/agent/tasks/<int:task_id>/stop", methods=["POST"])
@agent_auth()
def agent_stop_task(task_id):
    task = TaskRequest.query.get(task_id)
    if task and not task_belongs_to_caller(task):
        return jsonify({"error": "not your task"}), 403
    result = stop_task(task_id, reason="Agent Process Finished") 
    return jsonify(result), 200
    
@agent_bp.route("/agent/tasks/<int:task_id>/error", methods=["POST"])
@agent_auth()
def task_error(task_id):
    data = request.get_json()
    task = TaskRequest.query.get_or_404(task_id)
    if not task_belongs_to_caller(task):
        return jsonify({"error": "not your task"}), 403
    task.status = "failed"
    reason = data.get("reason", "Unknown agent error")
    log_event(task.id, task.assigned_node_id, "failed", f"Agent Error: {reason}")
    db.session.commit()
    return jsonify({"status": "noted", "error": reason}), 200
