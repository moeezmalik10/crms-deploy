import os
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

# Scheduling priority: lower runs first when the pool is contended. Interactive sessions
# (a student sitting at a VM/dev environment right now) outrank batch work (pool jobs,
# split jobs, ML training) that nobody is waiting in front of a screen for.
PRIORITY_INTERACTIVE = 10
PRIORITY_BATCH = 50


def default_priority(mode):
    return PRIORITY_INTERACTIVE if mode in ("physical", "remote") else PRIORITY_BATCH


# Node quarantine ("PCs that fail repeatedly are temporarily taken out of the pool"): a node
# crossing this many consecutive heartbeat-timeout failures is excluded from scheduling for a
# cooldown period, rather than becoming eligible again the instant a single heartbeat lands -
# a flapping PC would otherwise bounce work back and forth every cycle.
#
# A node is only ever re-flagged as failed once per distinct outage (cleanup_system's dead-node
# query requires status == "online", which the first detection already flips to "offline"), so
# "repeatedly" necessarily spans separate online/offline cycles. clear_node_failure() therefore
# can't reset the counter the instant any heartbeat lands - a node that goes down, reconnects
# for a few seconds, and drops again would have its failure history erased before a second
# outage could ever add to it, making the threshold unreachable in practice. Instead the streak
# only clears once the node has stayed healthy for a full stability window with no new failure.
QUARANTINE_FAILURE_THRESHOLD = int(os.environ.get("QUARANTINE_FAILURE_THRESHOLD", "3"))
QUARANTINE_COOLDOWN_SECONDS = int(os.environ.get("QUARANTINE_COOLDOWN_SECONDS", "600"))
QUARANTINE_STABILITY_WINDOW_SECONDS = int(os.environ.get("QUARANTINE_STABILITY_WINDOW_SECONDS", "300"))


def register_node_failure(node):
    """Call when a node is detected dead (heartbeat timeout). Tracks repeated failures and, once
    the node crosses the threshold, sets quarantined_until so lock_online_nodes()/
    fitting_device_count() stop offering it new work for the cooldown - even if it starts
    heartbeating again before the cooldown ends (see clear_node_failure)."""
    node.consecutive_failures = (node.consecutive_failures or 0) + 1
    node.last_failure_at = datetime.now(timezone.utc)
    if node.consecutive_failures >= QUARANTINE_FAILURE_THRESHOLD:
        node.quarantined_until = datetime.now(timezone.utc) + timedelta(seconds=QUARANTINE_COOLDOWN_SECONDS)


def clear_node_failure(node):
    """Call on every successful heartbeat/registration. Only resets the counter once the node
    has gone QUARANTINE_STABILITY_WINDOW_SECONDS since its last failure with no new one - a
    heartbeat landing sooner than that doesn't prove the flapping has stopped, so the streak is
    left alone and a failure shortly after still counts as "repeated." This deliberately does
    NOT clear an active quarantined_until: per the proposal a repeatedly-flapping PC is held out
    for the full cooldown (or until an admin clears it manually), not re-admitted the moment it
    next answers."""
    if not node.consecutive_failures:
        return
    last_failure = node.last_failure_at
    if last_failure is not None and last_failure.tzinfo is None:
        last_failure = last_failure.replace(tzinfo=timezone.utc)   # SQLite drops tzinfo
    if last_failure is None or (datetime.now(timezone.utc) - last_failure
                                 >= timedelta(seconds=QUARANTINE_STABILITY_WINDOW_SECONDS)):
        node.consecutive_failures = 0


def _not_quarantined():
    """Node.quarantined_until is NULL, or its cooldown has already passed."""
    now = datetime.now(timezone.utc)
    return db.or_(Node.quarantined_until.is_(None), Node.quarantined_until <= now)


def _consolidate(task, nodes, usage_map, active_tasks, profile):
    """Per the proposal: "work that cannot be divided is placed on the PC that fits it best;
    if no single PC has room, the system moves divisible work elsewhere to make room." Only
    ever evicts "job"-mode tasks (divisible - they restart cleanly on whatever device picks
    them up next) and never another physical/remote session, which is someone else's own
    non-divisible work and must not be touched to make room for this one.

    Only called once the normal scoring pass below already failed to find room as-is. Returns
    (node, jobs_to_evict) for the cheapest fix found (fewest jobs moved), or (None, []) if no
    node can be made to fit even by clearing every job off it. Disk capacity and the live
    psutil-reported free-RAM safety check (both checked in the main scoring loop) are not
    re-applied here - this handles the CPU/RAM case the proposal describes; a node blocked on
    disk or a live low-memory reading is left for the admin/next cycle rather than guessed at.
    """
    if task.mode not in ("physical", "remote"):
        return None, []

    best_node, best_evict = None, None
    for node in nodes:
        stats = usage_map[node.id]
        jobs_here = sorted(
            (t for t in active_tasks if t.assigned_node_id == node.id and t.mode == "job"),
            key=lambda t: -(t.required_ram_mb or 0))   # evict the biggest first - fewest moves
        if not jobs_here:
            continue   # nothing divisible here to move - eviction can't help this node

        other_session = any(t.assigned_node_id == node.id and t.mode in ("physical", "remote")
                            for t in active_tasks)
        if other_session and (task.mode == "physical" or stats["phys"] >= 1):
            continue   # someone else's session - not ours to move, physical needs the whole PC

        total_ram, total_cores = node.total_ram_mb or 0, node.total_cores or 0

        if task.mode == "physical":
            # Rule A (main loop) blocks physical placement on ANY busy device, job or not, so
            # every job here must go - not just enough of them to free up some numbers. But
            # the device still has to be big enough once it's entirely clear.
            cap_cpu, cap_ram = total_cores - total_cores * 0.05, total_ram - 768
            if cap_cpu < profile["cpu"] or cap_ram < profile["ram"]:
                continue   # empty or not, this device is simply too small for the session
            to_evict = jobs_here
        else:
            cap_cpu, cap_ram = total_cores - total_cores * 0.05, total_ram - 768
            if node.share_cores: cap_cpu = min(cap_cpu, node.share_cores)
            if node.share_ram_mb: cap_ram = min(cap_ram, node.share_ram_mb)

            cpu_used, ram_used, to_evict = stats["cpu"], stats["ram"], []
            fits = cap_cpu - cpu_used >= profile["cpu"] and cap_ram - ram_used >= profile["ram"]
            for j in jobs_here:
                if fits:
                    break
                cpu_used -= (j.required_cpu or 0)
                ram_used -= (j.required_ram_mb or 0)
                to_evict.append(j)
                fits = cap_cpu - cpu_used >= profile["cpu"] and cap_ram - ram_used >= profile["ram"]
            if not fits:
                continue

        if best_evict is None or len(to_evict) < len(best_evict):
            best_node, best_evict = node, to_evict
    return best_node, (best_evict or [])


def lock_online_nodes():
    """Online, recently-heard-from nodes, locked for the rest of this transaction. Every path
    that decides where resource-pool work lands - compute tasks here, ML chunks in
    spawn_distributed_ml, storage placement in _storage_targets - must lock its candidate
    nodes, or two concurrent allocators can each believe the same free capacity is available
    and both commit to using it (over-allocation). Concurrent callers block on these rows
    instead of using SKIP LOCKED, so one allocator waiting never sees an empty candidate set
    just because a peer is mid-decision.

    Always locked in the same order (by id) so two allocators that both need several of the
    same nodes never deadlock by acquiring them in opposite order."""
    timeout_limit = datetime.now(timezone.utc) - timedelta(seconds=45)
    return Node.query.filter(
        Node.status == 'online',
        Node.last_heartbeat > timeout_limit,
        _not_quarantined()
    ).order_by(Node.id).with_for_update(of=Node).all()


def compute_node_usage(node_ids, exclude_task_id=None):
    """The single source of truth for how booked each node is. Returns {node_id: stats}; stats
    splits "used" (status == running - actually consuming resources right now) from "reserved"
    (allocated/starting - promised to a task that hasn't started yet), which the scoring loop
    below treats identically (both reduce what's left to hand out) but the Unified Quota Ledger
    (refresh_ledger) persists separately, matching the proposal's own wording for Component 3.

    A physical session takes the WHOLE device under Rule A (see the mode checks in
    allocate_task and _consolidate), not just its own modest profile - so a node currently
    holding one is booked at its full total here. Scoring already blocks any other placement on
    such a node through the explicit mode checks before this dict's cpu/ram numbers are ever
    consulted, so this only changes what the ledger reports as free, never a placement
    decision."""
    q = TaskRequest.query.filter(
        TaskRequest.assigned_node_id.in_(node_ids),
        TaskRequest.assigned_node_id.isnot(None),
        TaskRequest.status.in_(["running", "starting", "allocated"]),
    )
    if exclude_task_id is not None:
        q = q.filter(TaskRequest.id != exclude_task_id)
    active_tasks = q.all()

    usage_map = {n_id: {"cpu": 0, "ram": 0, "disk": 0, "phys": 0, "rem": 0, "job": 0,
                         "used_cpu": 0, "used_ram": 0, "reserved_cpu": 0, "reserved_ram": 0}
                 for n_id in node_ids}
    physical_node_ids = {t.assigned_node_id for t in active_tasks if t.mode == "physical"}
    for t in active_tasks:
        usage = usage_map[t.assigned_node_id]
        usage["cpu"] += (t.required_cpu or 0)
        usage["ram"] += (t.required_ram_mb or 0)
        usage["disk"] += (t.required_disk_mb or 0)
        if t.mode == "physical": usage["phys"] += 1
        if t.mode == "remote": usage["rem"] += 1
        if t.mode == "job": usage["job"] += 1
        bucket = "used" if t.status == "running" else "reserved"
        usage[f"{bucket}_cpu"] += (t.required_cpu or 0)
        usage[f"{bucket}_ram"] += (t.required_ram_mb or 0)

    nodes_by_id = {n.id: n for n in Node.query.filter(Node.id.in_(node_ids)).all()} if physical_node_ids else {}
    for n_id in physical_node_ids:
        node = nodes_by_id.get(n_id)
        if not node:
            continue
        bucket = "used" if any(t.assigned_node_id == n_id and t.mode == "physical" and t.status == "running"
                                for t in active_tasks) else "reserved"
        usage_map[n_id]["used_cpu"] = usage_map[n_id]["used_ram"] = 0
        usage_map[n_id]["reserved_cpu"] = usage_map[n_id]["reserved_ram"] = 0
        usage_map[n_id][f"{bucket}_cpu"] = node.total_cores or 0
        usage_map[n_id][f"{bucket}_ram"] = node.total_ram_mb or 0
    return usage_map


def refresh_ledger(node_ids):
    """Persist compute_node_usage()'s numbers onto the Node rows themselves (the Unified Quota
    Ledger columns), inside the caller's already-locked transaction. Always recomputed as
    absolute values from TaskRequest, never incremented/decremented in place - an incremental
    counter drifts the moment one code path forgets to update it, which is the exact bug this
    replaces from the old ad-hoc quota checks. Caller must already hold the node row locks
    (e.g. via lock_online_nodes) so this can't race a concurrent allocator."""
    if not node_ids:
        return
    usage_map = compute_node_usage(node_ids)
    for node in Node.query.filter(Node.id.in_(node_ids)).all():
        stats = usage_map.get(node.id)
        if not stats:
            continue
        node.used_cores = stats["used_cpu"]
        node.used_ram_mb = stats["used_ram"]
        node.reserved_cores = stats["reserved_cpu"]
        node.reserved_ram_mb = stats["reserved_ram"]


def fitting_device_count(cpu, ram_mb):
    """How many online, job-capable devices could take one unit of this size right now, read
    from the Unified Quota Ledger columns (Node.used/reserved - kept fresh by refresh_ledger)
    rather than a fresh lock-and-recompute. This is a submit-time sizing hint for how many
    parts a plain split job is worth creating (see pool_routes.submit_job), not an allocation
    decision - the real, authoritative check (with the live psutil reading, row locks, and
    this same buffer/share-cap math) happens again when each part is actually placed, so a
    stale or slightly-off read here costs nothing worse than one part landing in the queue for
    a few seconds instead of running immediately."""
    count = 0
    for n in Node.query.filter(Node.status == "online", Node.device_key_hash.isnot(None),
                                Node.paused.is_(False), _not_quarantined()).all():
        if not (n.sandbox_mode in ("docker", "isolated") or
                (n.sandbox_mode == "light" and n.allow_light_sandbox)):
            continue
        cap_cpu = (n.total_cores or 0) - (n.total_cores or 0) * 0.05
        cap_ram = (n.total_ram_mb or 0) - 768
        if n.share_cores: cap_cpu = min(cap_cpu, n.share_cores)
        if n.share_ram_mb: cap_ram = min(cap_ram, n.share_ram_mb)
        avail_cpu = cap_cpu - (n.used_cores or 0) - (n.reserved_cores or 0)
        avail_ram = cap_ram - (n.used_ram_mb or 0) - (n.reserved_ram_mb or 0)
        if avail_cpu >= cpu and avail_ram >= ram_mb:
            count += 1
    return count


def higher_or_equal_priority_pending(priority, exclude_id=None, exclude_ids=None):
    """True if some other pending task would outrank (or tie) this one - used at submit time
    so a new request doesn't jump an immediate allocation ahead of a request that's already
    waiting its turn; it falls back to the priority-ordered queue (app.tasks.process_queue)
    instead. For a split job's children, pass exclude_ids=<all sibling ids> - they share one
    priority and must not count against each other."""
    q = TaskRequest.query.filter(TaskRequest.status == "pending", TaskRequest.priority <= priority)
    ids = list(exclude_ids) if exclude_ids is not None else ([exclude_id] if exclude_id is not None else [])
    if ids:
        q = q.filter(TaskRequest.id.notin_(ids))
    return db.session.query(q.exists()).scalar()


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
        
        # If already assigned, don't re-allocate. If it's moved on for any other reason - most
        # importantly, cancelled (DELETE /pool/jobs/<id> sets it to "failed" without ever
        # touching assigned_node_id) - don't touch it either. Without this, a task cancelled
        # while still queued and not yet picked up by this tick's process_queue batch gets
        # silently resurrected back to "pending" the moment this function's own "no capacity"
        # fallback runs, because that fallback unconditionally sets status = "pending" with no
        # idea the row had already been cancelled out from under it a moment earlier.
        if task.status != "pending":
            if task.assigned_node_id:
                return {"status": "already_allocated", "pc": task.assigned_pc}
            return {"status": task.status, "message": "task is no longer pending"}

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
        nodes = lock_online_nodes()

        # Browser devices ("WEB-...", from the Share this device page) can only be reserved
        # physically; remote (VM) sessions need a lab PC running the Python agent.
        if task.mode != "physical":
            nodes = [n for n in nodes if not (n.name or "").upper().startswith("WEB-")]
        # The owner paused sharing
        online_count = len(nodes)
        nodes = [n for n in nodes if not n.paused]
        # Sandboxed work needs a device that can isolate it
        no_node_reason = "No online nodes available. Queued."
        if task.mode == "job":
            nodes = [n for n in nodes if n.device_key_hash and (
                n.sandbox_mode in ("docker", "isolated") or (n.sandbox_mode == "light" and n.allow_light_sandbox))]
            if online_count and not nodes:
                no_node_reason = ("Queued: no online device can run jobs yet - a contributor needs the Windows sandbox "
                                  "(setup_sandbox.bat, no extra software) or Docker Desktop running.")
        elif task.mode == "remote":
            nodes = [n for n in nodes if n.sandbox_mode == "docker"]
            if online_count and not nodes:
                no_node_reason = ("Queued: workspaces need a device with Docker Desktop running; "
                                  f"{online_count} device(s) are online but none has Docker.")

        if not nodes:
            task.status = "pending"
            log_event(task.id, None, "queued", no_node_reason)
            db.session.commit()
            return {"status": "pending", "message": "No online nodes."}

        # BATCH RESOURCE CALCULATION (O(1) database complexity)
        node_ids = [n.id for n in nodes]
        usage_map = compute_node_usage(node_ids, exclude_task_id=task_id)
        active_tasks = TaskRequest.query.filter(
            TaskRequest.assigned_node_id.in_(node_ids),
            TaskRequest.assigned_node_id.isnot(None),
            TaskRequest.status.in_(["running", "starting", "allocated"]),
            TaskRequest.id != task_id
        ).all()

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
            # Jobs already piling onto this device count against it too - otherwise a node
            # running several sandboxed jobs looked exactly as attractive as an idle one.
            task_count = stats["phys"] + stats["rem"] + stats["job"]
            
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

            # The owner's share limits cap what this device lends to sandboxed work (jobs and
            # workspaces). A physical session reserves the whole device, as before.
            cap_cpu = total_cores - buffer_cores
            cap_ram = total_ram - buffer_ram
            if task.mode != "physical":
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

                # Parts of one split job go to different devices when possible, so they
                # really run in parallel instead of queueing for the same CPU.
                if task.parent_task_id:
                    siblings = sum(1 for t in active_tasks if t.assigned_node_id == node.id
                                   and t.parent_task_id == task.parent_task_id and t.id != task.id)
                    score -= siblings * 50000
                
                # live metric validation
                live_safe_ram = profile["ram"] + 768
                if latest_metric:
                    if latest_metric.memory_free_mb < live_safe_ram:
                        skipped.append(f"{node.name}: {latest_metric.memory_free_mb:.0f} MB free, needs {live_safe_ram} MB")
                        continue
                
                if score > highest_score:
                    highest_score = score
                    best_node = node

        # CONSOLIDATION: no PC had room as-is for this non-divisible session - see if moving
        # some divisible job(s) off one PC would make room there instead of queuing the session.
        consolidated_evictions = []
        if not best_node:
            best_node, consolidated_evictions = _consolidate(task, nodes, usage_map, active_tasks, profile)

        # EXECUTE ALLOCATION
        if not best_node:
            task.status = "pending"
            detail = "; ".join(skipped) or "no node qualified"
            log_event(task.id, None, "pending", f"Insufficient resources - {detail}")
            db.session.commit()
            return {"status": "pending"}

        evicted_node_ids = set()
        for j in consolidated_evictions:
            evicted_node_ids.add(j.assigned_node_id)
            j.status = "pending"
            j.assigned_node_id = None
            j.assigned_pc = None
            j.allocated_at = None
            j.message = f"Re-queued: moved off {best_node.name} to make room for task {task.id}'s {task.mode} session"
            log_event(j.id, None, "pending", j.message)

        task.assigned_node_id = best_node.id
        task.assigned_pc = best_node.name
        task.required_cpu = profile["cpu"]
        task.required_ram_mb = profile["ram"]
        if profile.get("disk"):
            task.required_disk_mb = profile["disk"]
        task.status = "allocated" # Waiting for Agent to start
        task.allocated_at = datetime.now(timezone.utc)

        # Unified Quota Ledger: persist the new numbers for this node and any node a job was
        # just evicted from, while we still hold their row locks from lock_online_nodes().
        refresh_ledger({best_node.id, *evicted_node_ids})

        if consolidated_evictions:
            log_event(task.id, best_node.id, "allocated",
                     f"Assigned to {best_node.name} after moving {len(consolidated_evictions)} "
                     f"job(s) elsewhere to make room")
        else:
            log_event(task.id, best_node.id, "allocated", f"Assigned to {best_node.name}")

        # Atomic commit: All logs + Task updates + Node lock release
        db.session.commit()

        return {
            "status": "allocated",
            "assigned_machine": best_node.name,
            "mode": task.mode,
            "consolidated": len(consolidated_evictions),
        }

    except Exception as e:
        db.session.rollback()
        return {"error": "Internal allocation failure", "details": str(e)}

def stop_task(task_id, reason="Requested"):
    #Frees resources by marking task as completed and logs the stop event with a reason.
    task = TaskRequest.query.get(task_id)
    if not task or task.status == "completed":
        return {"status": "skipped", "reason": "Task not found or already stopped"}

    node_id = task.assigned_node_id
    if node_id:
        # Lock the node row before touching its ledger columns, same discipline as allocate_task.
        Node.query.with_for_update().get(node_id)

    task.status = "completed"
    task.completed_at = datetime.now(timezone.utc)

    log_event(task.id, task.assigned_node_id, "completed", f"Stopped: {reason}")
    if node_id:
        refresh_ledger([node_id])
    db.session.commit()

    return {"status": "stopped", "task_id": task.id}


def release_node_tasks(node, reason):
    """A node left or stopped answering. Session requests it had not started yet go back to
    the queue so another PC / device can take them. Pool-job work (mode == "job") is stateless
    and restartable - the whole unit reruns from scratch wherever it lands next - so it
    requeues even if it was already "running" here: only that interrupted unit repeats, per
    the proposal's lease-based recovery. Physical/remote sessions and ML federated rounds are
    stateful and can't be silently resumed on another device, so those still fail outright.

    This can falsely trigger on a node that's actually still fine (a dropped heartbeat, not a
    real failure - see app.tasks.cleanup_system's timeout). The old node finishing its stale
    run afterwards is harmless: app.pool_agent_routes.job_result() only accepts a result from
    whichever node is current in assigned_node_id, which this function has already moved on,
    so a late stale result is rejected rather than silently overwriting the real one."""
    requeued, failed = 0, 0
    tasks = TaskRequest.query.filter(
        TaskRequest.assigned_node_id == node.id,
        TaskRequest.status.in_(["allocated", "starting", "running"])
    ).all()
    for t in tasks:
        is_ml = "ml" in (t.task_type or "")
        resumable = not is_ml and (t.mode == "job" or t.status in ("allocated", "starting"))
        if resumable:
            log_event(t.id, node.id, "pending", f"Re-queued: {reason} ({node.name})")
            t.status = "pending"
            t.assigned_node_id = None
            t.assigned_pc = None
            t.allocated_at = None
            t.start_time = None
            t.expiry_time = None
            t.exit_code = None
            t.output_tail = None
            t.result_blob_id = None
            t.message = f"Re-queued: {reason}"
            requeued += 1
        else:
            t.status = "failed"
            t.message = f"Failed: {reason} ({node.name})"
            log_event(t.id, node.id, "failed", t.message)
            failed += 1
            # A federated-learning round can never finish without this chunk, so the
            # parent job must not be left waiting forever for a "completed" that will
            # never come - fail it now instead of hanging in "running" indefinitely.
            if is_ml and t.parent_task_id:
                parent = TaskRequest.query.get(t.parent_task_id)
                if parent and parent.status not in ("completed", "failed"):
                    parent.status = "failed"
                    parent.message = f"Failed: child task {t.id} lost its device ({reason})"
                    log_event(parent.id, None, "failed", parent.message)
    refresh_ledger([node.id])
    return requeued, failed
