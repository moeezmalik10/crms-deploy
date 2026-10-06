from datetime import datetime, timezone, timedelta
from app import db
from app.models import Node, TaskRequest, TaskExecutionLog, NodeMetrics

# Raw resource requirements per task type
TASK_PROFILES = {
    "dev_cpp":        {"cpu": 1, "ram": 2048},
    "ubuntu":         {"cpu": 2, "ram": 4096},
    "anylogic":       {"cpu": 4, "ram": 8192},
    "vs_code":        {"cpu": 2, "ram": 4096},
    "visual_studio":  {"cpu": 4, "ram": 8192},
    "android_studio": {"cpu": 4, "ram": 8192},
    "oracle":         {"cpu": 2, "ram": 4096},
    "sumo":           {"cpu": 2, "ram": 4096},
    "ml_node":        {"cpu": 2, "ram": 4096} 
}

# Disk a sandboxed workspace may use on the device (MB)
WORKSPACE_DISK_MB = 3072


def log_event(task_id, node_id, status, message):
    #Log task execution event followed by db.session.commit() in caller.
    try:
        log = TaskExecutionLog(
            task_id=task_id,
            node_id=node_id,
            status=status,
            message=message,
            timestamp=datetime.now(timezone.utc)
        )
        db.session.add(log)
        print(f"LOG SAVED: Task {task_id} -> {status}: {message}")
        return log
    except Exception as e:
        print(f"ERROR logging task {task_id}: {e}")
        raise  # Re-raise so caller knows logging failed
        
def allocate_task(task_id):
    # PERFORMS:
    # Row-level locking on Task & Nodes
    # Batch calculation of resources for all candidates (O(1) DB queries)   
    # Scoring with multi-tenancy guards and safety buffers
    # Concurrent allocators block on the same node rows (no skip_locked) so we
    # never return "no nodes" while peers hold locks on all online machines.
    # Automatic rollback on failure
    
    try:
        # TASK LOCKING: Prevents two threads from processing the same task
        task = TaskRequest.query.with_for_update().get(task_id)
        if not task:
            return {"error": "Task not found"}
        
        # If already assigned, don't re-allocate
        if task.assigned_node_id and task.status != "pending":
            return {"status": "already_allocated", "pc": task.assigned_pc}

        if task.mode == "job":
            # Compute jobs ask for exactly what they need (chosen by the student)
            profile = {"cpu": task.required_cpu or 1, "ram": task.required_ram_mb or 1024,
                       "disk": task.required_disk_mb or 500}
        else:
            profile = dict(TASK_PROFILES.get(task.task_type) or {})
            if not profile:
                return {"error": f"Task type '{task.task_type}' not found"}
            if task.mode == "remote":
                profile["disk"] = WORKSPACE_DISK_MB

        # Finding ELIGIBLE NODES — lock all candidates; peers wait instead of SKIP LOCKED → empty set
        timeout_limit = datetime.now(timezone.utc) - timedelta(seconds=45)

        nodes = Node.query.filter(
            Node.status == 'online',
            Node.last_heartbeat > timeout_limit
        ).with_for_update(of=Node).all()

        # Browser devices ("WEB-...", from the Share this device page) can only be reserved
        # physically; remote (VM) sessions need a lab PC running the Python agent.
        if task.mode != "physical":
            nodes = [n for n in nodes if not (n.name or "").upper().startswith("WEB-")]
        # The owner paused sharing
        nodes = [n for n in nodes if not n.paused]
        # Sandboxed work needs a device that can isolate it
        if task.mode == "job":
            nodes = [n for n in nodes if n.device_key_hash and (
                n.sandbox_mode == "docker" or (n.sandbox_mode == "light" and n.allow_light_sandbox))]
        elif task.mode == "remote":
            nodes = [n for n in nodes if n.sandbox_mode == "docker"]

        if not nodes:
            task.status = "pending"
            log_event(task.id, None, "queued", "No online nodes available. Queued.")
            db.session.commit()
            return {"status": "pending", "message": "No online nodes."}

        # BATCH RESOURCE CALCULATION (O(1) database complexity)
        node_ids = [n.id for n in nodes]
        active_tasks = TaskRequest.query.filter(
            TaskRequest.assigned_node_id.in_(node_ids),
            TaskRequest.assigned_node_id.isnot(None),
            TaskRequest.status.in_(["running", "starting", "allocated"]),
            TaskRequest.id != task_id
        ).all()

        usage_map = {n_id: {"cpu": 0, "ram": 0, "disk": 0, "phys": 0, "rem": 0} for n_id in node_ids}
        for t in active_tasks:
            usage = usage_map[t.assigned_node_id]
            usage["cpu"] += (t.required_cpu or 0)
            usage["ram"] += (t.required_ram_mb or 0)
            usage["disk"] += (t.required_disk_mb or 0)
            if t.mode == "physical": usage["phys"] += 1
            if t.mode == "remote": usage["rem"] += 1

        metrics = NodeMetrics.query.filter(
            NodeMetrics.node_id.in_(node_ids)
        ).order_by(NodeMetrics.timestamp.desc()).all()

        metric_map = {}
        for m in metrics:
            if m.node_id not in metric_map:
                metric_map[m.node_id] = m
                
        # SCORING ENGINE
        best_node = None
        highest_score = float('-inf')
        skipped = []   # why each node was not chosen, shown to the admin

        for node in nodes:
            latest_metric = metric_map.get(node.id)

            if latest_metric:        
                # If the PC physically reports less than 2GB free right now, skip it, even if database says it's empty.
                if latest_metric.memory_free_mb < 2048:
                    skipped.append(f"{node.name}: only {latest_metric.memory_free_mb:.0f} MB RAM free")
                    continue
            stats = usage_map[node.id]
            task_count = stats["phys"] + stats["rem"]
            
            # Rule A: a physical reservation takes the whole device. Sandboxed work (jobs and
            # workspaces) only takes a slice, so several can share one device - but not while
            # someone has the device reserved in person.
            busy = [f"{t.id} ({t.mode})" for t in active_tasks if t.assigned_node_id == node.id]
            if task.mode == "physical" and busy:
                skipped.append(f"{node.name}: in use by task {', '.join(busy)}")
                continue
            if task.mode != "physical" and stats["phys"] >= 1:
                skipped.append(f"{node.name}: reserved in person (physical session)")
                continue

            # Rule B: Safety Buffer (20% of total)
            total_ram = node.total_ram_mb or 0
            total_cores = node.total_cores or 0

            # Detecting if DB stats are corrupted (Used > Total)
            if stats["ram"] > total_ram or stats["cpu"] > total_cores:
                print(f"SANITY WARNING: Node {node.name} reports over-capacity usage!")

            buffer_ram = 768
            buffer_cores = total_cores * 0.05

            # The owner's share limits cap what this device lends
            cap_cpu = total_cores - buffer_cores
            cap_ram = total_ram - buffer_ram
            if node.share_cores:
                cap_cpu = min(cap_cpu, node.share_cores)
            if node.share_ram_mb:
                cap_ram = min(cap_ram, node.share_ram_mb)
            avail_cpu = max(0, cap_cpu - stats["cpu"])
            avail_ram = max(0, cap_ram - stats["ram"])

            # Disk for sandboxed work: keep 5 GB free for the owner
            need_disk = profile.get("disk") or 0
            if need_disk:
                free_disk = (node.free_storage_gb or 0) * 1024 - 5120 - stats["disk"]
                if free_disk < need_disk:
                    skipped.append(f"{node.name}: {max(0, free_disk):.0f} MB disk free to lend, needs {need_disk:.0f} MB")
                    continue

            #validation
        
            if not (avail_cpu >= profile["cpu"] and avail_ram >= profile["ram"]):
                skipped.append(f"{node.name}: capacity left {avail_cpu:.1f} cores / {avail_ram:.0f} MB, needs {profile['cpu']:g} cores / {profile['ram']:.0f} MB")
            if avail_cpu >= profile["cpu"] and avail_ram >= profile["ram"]:
                # Resource utilization
                remaining_ram = avail_ram - profile["ram"]

                future_ram_usage = (
                    (stats["ram"] + profile["ram"]) / total_ram
                    if total_ram else 1
                )
                
                future_cpu_usage = (
                    (stats["cpu"] + profile["cpu"]) / total_cores
                    if total_cores else 1
                )
                
                score = 0
                
                # Prefer efficient packing
                score -= remaining_ram * 0.05
                
                # Avoid overloaded nodes
                score -= future_ram_usage * 4000
                score -= future_cpu_usage * 2000
                
                # Mild fairness
                score -= task_count * 300
                
                # live metric validation
                live_safe_ram = profile["ram"] + 768
                if latest_metric:
                    if latest_metric.memory_free_mb < live_safe_ram:
                        skipped.append(f"{node.name}: {latest_metric.memory_free_mb:.0f} MB free, needs {live_safe_ram} MB")
                        continue
                
                if score > highest_score:
                    highest_score = score
                    best_node = node

        # EXECUTE ALLOCATION
        if not best_node:
            task.status = "pending"
            detail = "; ".join(skipped) or "no node qualified"
            log_event(task.id, None, "pending", f"Insufficient resources - {detail}")
            db.session.commit()
            return {"status": "pending"}

        task.assigned_node_id = best_node.id
        task.assigned_pc = best_node.name
        task.required_cpu = profile["cpu"]
        task.required_ram_mb = profile["ram"]
        if profile.get("disk"):
            task.required_disk_mb = profile["disk"]
        task.status = "allocated" # Waiting for Agent to start
        
        log_event(task.id, best_node.id, "allocated", f"Assigned to {best_node.name}")
        
        # Atomic commit: All logs + Task updates + Node lock release
        db.session.commit()

        return {
            "status": "allocated",
            "assigned_machine": best_node.name,
            "mode": task.mode
        }

    except Exception as e:
        db.session.rollback()
        return {"error": "Internal allocation failure", "details": str(e)}

def stop_task(task_id, reason="Requested"):
    #Frees resources by marking task as completed and logs the stop event with a reason.
    task = TaskRequest.query.get(task_id)
    if not task or task.status == "completed":
        return {"status": "skipped", "reason": "Task not found or already stopped"}

    task.status = "completed"
    task.completed_at = datetime.now(timezone.utc)
    
    log_event(task.id, task.assigned_node_id, "completed", f"Stopped: {reason}")
    db.session.commit()
    
    return {"status": "stopped", "task_id": task.id}


def release_node_tasks(node, reason):
    """A node left or stopped answering. Session requests it had not started yet go back to
    the queue so another PC / device can take them; work already running there fails."""
    requeued, failed = 0, 0
    tasks = TaskRequest.query.filter(
        TaskRequest.assigned_node_id == node.id,
        TaskRequest.status.in_(["allocated", "starting", "running"])
    ).all()
    for t in tasks:
        is_ml = "ml" in (t.task_type or "")
        if t.status in ("allocated", "starting") and not is_ml:
            log_event(t.id, node.id, "pending", f"Re-queued: {reason} ({node.name})")
            t.status = "pending"
            t.assigned_node_id = None
            t.assigned_pc = None
            t.message = f"Re-queued: {reason}"
            requeued += 1
        else:
            t.status = "failed"
            t.message = f"Failed: {reason} ({node.name})"
            log_event(t.id, node.id, "failed", t.message)
            failed += 1
    return requeued, failed
