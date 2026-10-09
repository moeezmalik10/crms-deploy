from datetime import datetime, timezone, timedelta
from app import db, scheduler
from app.models import TaskRequest, Node, NodeMetricsHistory
from app.allocation_engine import allocate_task, stop_task, release_node_tasks, log_event

METRICS_HISTORY_DAYS = 7

@scheduler.task('interval', id='process_queue', seconds=15) # Increased to 15s
def process_queue():
    with scheduler.app.app_context():
        try:
            # Highest priority first (lower number = served first), then oldest first within
            # a priority tier. A capped batch, not just the oldest 3: with a fixed limit(3)
            # ordered only by created_at, 3 requests stuck waiting for capacity would silently
            # block every other pending request behind them from ever being attempted, no
            # matter how small or how long it had been waiting.
            queued_tasks = TaskRequest.query.filter(
                TaskRequest.status == "pending",
                TaskRequest.assigned_node_id.is_(None)
            ).order_by(TaskRequest.priority.asc(), TaskRequest.created_at.asc()).limit(20).all()

            for task in queued_tasks:
                # Use a try block inside the loop so one bad task doesn't kill the whole queue
                try:
                    allocate_task(task.id)
                except Exception as inner_e:
                    db.session.rollback()
                    print(f"Task {task.id} allocation failed: {inner_e}")

        except Exception as e:
            db.session.rollback()
            print(f"Global Scheduler Error: {e}")
        finally:
            db.session.remove() # Releases the DB connection back to the pool

@scheduler.task('interval', id='cleanup_system', seconds=30)
def cleanup_system():
    with scheduler.app.app_context():
        now = datetime.now(timezone.utc)
        # Requeue tasks that were handed to an agent but never acknowledged as ready.
        # This prevents jobs getting stuck in "starting" if an agent goes offline/crashes.
        stale_starting_limit = now - timedelta(minutes=5)
        stale_starting = TaskRequest.query.filter(
            TaskRequest.status == "starting",
            TaskRequest.created_at < stale_starting_limit
        ).all()
        for task in stale_starting:
            if "ml" in (task.task_type or ""):
                continue
            task.status = "pending"
            task.assigned_node_id = None   # let the queue place it on any node again
            task.assigned_pc = None
            task.message = "Re-queued after starting timeout (agent did not acknowledge ready)."
            print(f"Task {task.id} re-queued after starting timeout.")

        # Fail tasks on 'Ghost Nodes' (Agent stopped heartbeating)
        timeout_limit = now - timedelta(seconds=60)
        dead_nodes = Node.query.filter(Node.status == "online", Node.last_heartbeat < timeout_limit).all()

        for node in dead_nodes:
            node.status = "offline"
            requeued, failed = release_node_tasks(node, "node heartbeat timeout")
            print(f"Node {node.name} marked offline. {requeued} task(s) re-queued, {failed} failed.")
        
        running_tasks = TaskRequest.query.filter_by(status="running").all()
        for t in running_tasks:
            # If there is no expiry_time (ML tasks), skip the time-out check entirely
            if t.task_type in ["ml_task", "ml_job_parent"] or t.expiry_time is None:
                continue
            if t.mode == "job":
                # The agent stops a job at its time limit and reports; this is the safety net
                if now > t.expiry_time + timedelta(minutes=3):
                    t.status = "failed"
                    t.completed_at = now
                    t.message = "Failed: time limit reached and the device did not report back"
                    log_event(t.id, t.assigned_node_id, "failed", t.message)
                continue
            if now > t.expiry_time:
                try:
                    stop_task(t.id, reason="Session Expired")
                except Exception as e:
                    print(f"Failed to stop expired task {t.id}: {e}")
                    db.session.rollback()  # Rollback any partial changes from stop_task
        
        db.session.commit()


@scheduler.task('interval', id='pool_storage_upkeep', seconds=60)
def pool_storage_upkeep():
    """Keep pooled files healthy: add copies on newly available devices, drop server copies
    once a download is over, and finish deletes of files whose devices are gone."""
    from app.models import PoolBlob, PoolChunk, PoolFile, PoolReplica, DeviceEnrollment
    from app.pool_routes import _storage_targets
    from app.pool_agent_routes import _maybe_finish_replication
    with scheduler.app.app_context():
        try:
            now = datetime.now(timezone.utc)
            for f in PoolFile.query.filter(PoolFile.status != "deleting").all():
                # A finished download: the server copies are no longer needed
                req = f.download_requested_at
                if req is not None:
                    req = req if req.tzinfo else req.replace(tzinfo=timezone.utc)
                    if now - req > timedelta(minutes=15):
                        f.download_requested_at = None
                # Missing copies: place them on devices that do not hold this file yet
                for c in f.chunks:
                    live = [r for r in c.replicas if r.status in ("pending", "stored")]
                    missing = (f.replicas_wanted or 1) - len(live)
                    if missing > 0 and c.tmp_blob_id:
                        for n in _storage_targets(c.size, exclude={r.node_id for r in c.replicas})[:missing]:
                            db.session.add(PoolReplica(chunk_id=c.id, node_id=n.id, status="pending"))
                db.session.flush()
                _maybe_finish_replication(f)
            # Server copies of chunks that are no longer referenced
            for c in PoolChunk.query.filter(PoolChunk.tmp_blob_id.isnot(None)).all():
                if c.file.status == "deleting":
                    PoolBlob.query.filter_by(id=c.tmp_blob_id).delete()
                    c.tmp_blob_id = None
            # Old join codes
            DeviceEnrollment.query.filter(DeviceEnrollment.expires_at < now - timedelta(days=1)).delete()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Pool storage upkeep error: {e}")
        finally:
            db.session.remove()


@scheduler.task('interval', id='trim_metrics_history', hours=1)
def trim_metrics_history():
    """Keep the pool-dashboard usage history to a rolling window instead of growing forever -
    the live NodeMetrics row for each node is untouched, only the sampled history trail."""
    with scheduler.app.app_context():
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=METRICS_HISTORY_DAYS)
            NodeMetricsHistory.query.filter(NodeMetricsHistory.timestamp < cutoff).delete()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Metrics history trim error: {e}")
        finally:
            db.session.remove()
