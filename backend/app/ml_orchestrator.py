import os
import json
import uuid
import pandas as pd
import numpy as np
from app import db
from app.models import TaskRequest, MLResult
from app.allocation_engine import TASK_PROFILES, lock_online_nodes, log_event
from datetime import datetime, timezone
from supabase import create_client

def _nodes_meeting_ml_profile(nodes):
    #Same minimum hardware bar as TASK_PROFILES['ml_node'] for  consistency. 
    # In a real system, we might want dynamic profiles or more granular requirements per ML model type.
    prof = TASK_PROFILES.get("ml_node", {"cpu": 2, "ram": 4096})
    need_cpu, need_ram = prof["cpu"], prof["ram"]
    # Browser nodes ("WEB-..." phones / tablets joined from the Share this device page) cannot
    # run scikit-learn, so ML chunks only go to PCs running the Python agent.
    return [
        n for n in nodes
        if (n.total_cores or 0) >= need_cpu and (n.total_ram_mb or 0) >= need_ram
        and not (n.name or "").upper().startswith("WEB-")
        and not n.paused
    ]

def _safe_task_message_json(msg):
    if not msg:
        return {}
    try:
        return json.loads(msg)
    except (json.JSONDecodeError, TypeError):
        return {}


def _persist_split_dataset(df, user_id):
    # Agents slice by start_row/end_row on dataset_url. 
   
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    uid = str(user_id).strip() or "anonymous"
    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not (supabase_url and supabase_key):
        raise RuntimeError("SUPABASE_URL/SUPABASE_KEY are required for distributed ML data access")

    client = create_client(supabase_url, supabase_key)
    path = f"user_{uid}/split_{uuid.uuid4().hex}.csv"
    client.storage.from_("datasets").upload(path, csv_bytes)
    return client.storage.from_("datasets").get_public_url(path)


def spawn_distributed_ml(user_id, dataset_url, model_type, validation_type, split_mode="iid"):
    # Identify Online Nodes that meet ML slot requirements. Locked the same way allocate_task
    # locks its candidates, so this can't race a compute-task allocation onto the same node.
    candidates = lock_online_nodes()
    nodes = _nodes_meeting_ml_profile(candidates)

    # Never place an ML chunk on a node that is already running a physical/remote student
    # session - allocate_task() enforces "one physical session per PC at a time" for those
    # modes, and this must not silently collide with it just because it takes a different path.
    if nodes:
        busy_node_ids = {
            t.assigned_node_id for t in TaskRequest.query.filter(
                TaskRequest.assigned_node_id.in_([n.id for n in nodes]),
                TaskRequest.status.in_(["running", "starting", "allocated"]),
                TaskRequest.mode.in_(["physical", "remote"]),
            ).all()
        }
        nodes = [n for n in nodes if n.id not in busy_node_ids]

    if not nodes:
        return {"error": "No lab PCs are currently online with enough CPU/RAM for ML workers."}

    # Analyze Dataset (Count Rows) - Verify BEFORE creating tasks
    try:
        df = pd.read_csv(dataset_url)
        columns = list(df.columns)
        feature_columns = columns[:-1]
        target_column = columns[-1]
    except Exception as e:
        return {"error": f"CSV Error: {str(e)}"}

    mode = (split_mode or "iid").strip().lower().replace("-", "_")
    if mode == "iid":
        # Per-job random seed keeps runs reproducible in metadata and truly random across jobs.
        shuffle_seed = int(np.random.default_rng().integers(0, 2**32 - 1))
        df = df.sample(frac=1, random_state=shuffle_seed).reset_index(drop=True)
    elif mode == "non_iid":
        shuffle_seed = None
        df = df.sort_values(by=target_column).reset_index(drop=True)
    else:
        return {"error": "Invalid split_mode. Use 'iid' or 'non_iid'"}

    total_rows = len(df)
    try:
        training_dataset_url = _persist_split_dataset(df, user_id)
    except Exception as e:
        return {"error": f"Could not publish split dataset: {str(e)}"}

    # Create the PARENT Task (The Job)
    # Adding the federated payload ensures the 2-round logic can start
    fed_payload = {
        "round": 1,
        "weights": None,
        "intercept": None,
        "feature_columns": feature_columns,
        "target_column": target_column,
        "split_mode": mode,
        "shuffle_seed": shuffle_seed,
    }
    
    parent_job = TaskRequest(
        user_id=user_id,
        task_type="ml_job_parent",
        status="running",
        dataset_url=training_dataset_url,
        model_type=model_type,
        message=json.dumps(fed_payload) # Essential for Federated Averaging
    )
    db.session.add(parent_job)
    db.session.flush() # Get parent_job.id before committing so child tasks can reference it in parent_task_id
    
    # Split and Assign to Nodes
    job_id = parent_job.id
    num_nodes = len(nodes)
    rows_per_node = total_rows // num_nodes
    
    # Log parent job creation
    log_event(parent_job.id, None, "running", f"ML Job created: {model_type} on {num_nodes} nodes, {total_rows} rows")
    
    if rows_per_node == 0:
        return {"error": "Dataset has fewer rows than available nodes. Cannot split into empty chunks."}
    
    for i, node in enumerate(nodes):
        child_task = TaskRequest(
            user_id=user_id,
            parent_task_id=job_id,
            assigned_node_id=node.id,
            assigned_pc=node.name,
            task_type="ml_task",
            status="allocated",
            dataset_url=training_dataset_url,
            model_type=model_type,
            validation_type=validation_type,
            chunk_id=i+1,
            start_row=i * rows_per_node,
            end_row=total_rows if i == num_nodes - 1 else (i + 1) * rows_per_node,
            message=json.dumps(fed_payload)
        )
        db.session.add(child_task)
        db.session.flush()  # give child_task an id before it is logged
        # Log child task creation
        log_event(child_task.id, node.id, "allocated", f"ML Task assigned to {node.name}: chunk {i+1}, rows {child_task.start_row}-{child_task.end_row}")

    db.session.commit()
    
    # RETURN ALL KEYS to prevent frontend "undefined" errors
    return {
        "status": "success",
        "id": job_id, 
        "job_id": job_id,
        "message": f"Dataset split into {num_nodes} chunks. Round 1 initiated."
    }

    
def aggregate_ml_results(parent_task_id):
    # FEDERATED AGGREGATION LOGIC:
    # Waits for all child tasks to complete.
    # Averages weights/intercepts using Numpy.
    # - Round 1 Completion: Resets tasks for Round 2 with global weights.
    # - Round 2 Completion: Finalizes the model and saves the downloadable artifact.

    parent_job = TaskRequest.query.get(parent_task_id)
    if not parent_job:
        return None

    children = TaskRequest.query.filter_by(parent_task_id=parent_task_id).all()

    # A child that failed (e.g. its device left the pool) will never become "completed" -
    # fail the whole job now instead of waiting forever for a result that can't arrive.
    failed_children = [c for c in children if c.status == "failed"]
    if failed_children and parent_job.status not in ("completed", "failed"):
        parent_job.status = "failed"
        parent_job.message = f"Failed: child task {failed_children[0].id} did not complete"
        log_event(parent_job.id, None, "failed", parent_job.message)
        db.session.commit()
        return None

    # Ensure all nodes have finished the current round
    if not children or any(c.status != 'completed' for c in children):
        return None

    # Retrieve current round from parent metadata
    parent_meta = _safe_task_message_json(parent_job.message)
    current_round = parent_meta.get("round", 1)
    feature_columns = parent_meta.get("feature_columns", [])
    target_column = parent_meta.get("target_column", "target")

    all_weights = []
    all_intercepts = []
    sum_acc = sum_prec = sum_rec = sum_f1 = 0
    total_nodes = len(children)

    # Extract training results from children
    for child in children:
        child_data = _safe_task_message_json(child.message)
        
        # Collect parameters for averaging
        if child_data.get("weights") is not None:
            all_weights.append(child_data["weights"])
            all_intercepts.append(child_data["intercept"])
        
        # Accumulate metrics (relevant for the final report)
        metrics = child_data.get("metrics", {})
        sum_acc += metrics.get("accuracy", 0)
        sum_prec += metrics.get("precision", 0)
        sum_rec += metrics.get("recall", 0)
        sum_f1 += metrics.get("f1_score", 0)

    if not all_weights:
        print(f"Warning: No weights received for Job {parent_task_id}")
        return None

    # Compute Federated Average (FedAvg) using Numpy
    avg_weights = np.mean(all_weights, axis=0).tolist()
    avg_intercept = np.mean(all_intercepts, axis=0).tolist()

    if current_round == 1:
        # TRANSITION: ROUND 1 -> ROUND 2 -
        print(f"Round 1 completed for Job {parent_task_id} → starting Round 2")
        
        round_2_payload = {
            "round": 2,
            "weights": avg_weights,
            "intercept": avg_intercept,
            # Preserve metadata across rounds so final artifact remains complete.
            "feature_columns": feature_columns,
            "target_column": target_column,
            "split_mode": parent_meta.get("split_mode", "iid"),
            "shuffle_seed": parent_meta.get("shuffle_seed"),
        }
        
        # Update parent state to Round 2
        parent_job.message = json.dumps(round_2_payload)

        # Reset child tasks to 'allocated' so nodes pull them again for Round 2
        # The new message contains the global weights for local fine-tuning
        for child in children:
            child.status = "allocated"
            child.message = json.dumps(round_2_payload)
        
        # Log round transition
        log_event(parent_job.id, None, "running", f"Round 1 completed: {total_nodes} nodes, avg_accuracy={sum_acc/total_nodes:.3f}. Starting Round 2.")
        
        db.session.commit()

    elif current_round == 2:
        # FINALIZATION: ROUND 2 COMPLETE 
        print(f"Final model created for Job {parent_task_id}")
        
        # The final model artifact for the user
        model_artifact = {
            "model_metadata": {
                "job_id": parent_task_id,
                "model_type": parent_job.model_type,
                "training_type": "2-Round Federated Averaging",
                "nodes_participated": total_nodes,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "feature_columns": feature_columns,
                "target_column": target_column
            },
            "performance_metrics": {
                "accuracy": sum_acc / total_nodes,
                "precision": sum_prec / total_nodes,
                "recall": sum_rec / total_nodes,
                "f1_score": sum_f1 / total_nodes
            },
            "model_parameters": {
                "weights": avg_weights,
                "intercept": avg_intercept
            }
        }

        # Save to MLResult table for user download
        final_result = MLResult(
            task_id=parent_task_id,
            accuracy=model_artifact["performance_metrics"]["accuracy"],
            precision=model_artifact["performance_metrics"]["precision"],
            recall=model_artifact["performance_metrics"]["recall"],
            f1_score=model_artifact["performance_metrics"]["f1_score"],
            feature_importance=json.dumps(model_artifact) # Final JSON model file content
        )

        parent_job.status = "completed"
        db.session.add(final_result)
        
        # Log final completion
        log_event(parent_job.id, None, "completed", 
                 f"ML Job completed: {total_nodes} nodes, final_accuracy={model_artifact['performance_metrics']['accuracy']:.3f}, f1={model_artifact['performance_metrics']['f1_score']:.3f}")
        
        db.session.commit()
