from datetime import datetime, timezone, timedelta
from app import db, scheduler
from app.models import TaskRequest, Node
from app.allocation_engine import allocate_task, stop_task, release_node_tasks

@scheduler.task('interval', id='process_queue', seconds=15) # Increased to 15s
def process_queue():
    with scheduler.app.app_context():
        try:
            # Only attempt 3 tasks at a time to keep the loop fast
            queued_tasks = TaskRequest.query.filter(
                TaskRequest.status == "pending",
                TaskRequest.assigned_node_id.is_(None)
            ).order_by(TaskRequest.created_at.asc()).limit(3).all()

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
            if now > t.expiry_time:
                try:
                    stop_task(t.id, reason="Session Expired")
                except Exception as e:
                    print(f"Failed to stop expired task {t.id}: {e}")
                    db.session.rollback()  # Rollback any partial changes from stop_task
        
        db.session.commit()
