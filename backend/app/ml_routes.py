import os
import uuid
import json
import math
import traceback
from flask import Response
from flask import Blueprint, request, jsonify
from app import db
from app.models import TaskRequest, MLResult
from app.ml_orchestrator import spawn_distributed_ml, aggregate_ml_results
from app.allocation_engine import log_event
from supabase import create_client

ml_bp = Blueprint('ml', __name__)

# =========================
# SUPABASE CONFIG
# =========================
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Supabase connected")
    except Exception as e:
        print(f"Supabase init error: {e}")
else:
    print("Supabase not configured")


def _flatten_weights(weights):
    if weights is None:
        return None
    if (
        isinstance(weights, list)
        and len(weights) == 1
        and isinstance(weights[0], list)
    ):
        return weights[0]
    return weights


def _scalar_intercept(intercept):
    if intercept is None:
        return None
    if isinstance(intercept, (list, tuple)):
        return float(intercept[0]) if len(intercept) else 0.0
    return float(intercept)


def _local_python_snippet(model_type, weights_flat, bias, feature_order):
    #Runnable copy-paste code for users who do not call the predict API.
    w_json = json.dumps(weights_flat)
    b_json = json.dumps(bias)
    order_json = json.dumps(feature_order)
    if model_type == "logistic_regression":
        return (
            "import math\n"
            f"FEATURE_ORDER = {order_json}\n"
            f"WEIGHTS = {w_json}\n"
            f"INTERCEPT = {b_json}\n\n"
            "def predict_proba(x):\n"
            "    z = sum(w * xi for w, xi in zip(WEIGHTS, x)) + INTERCEPT\n"
            "    return 1.0 / (1.0 + math.exp(-z))\n\n"
            "def predict_class(x, threshold=0.5):\n"
            "    return 1 if predict_proba(x) >= threshold else 0\n\n"
            "# Replace sample_x with your values in the same order as FEATURE_ORDER\n"
            "sample_x = [0.0] * len(FEATURE_ORDER)\n"
            'print("probability:", predict_proba(sample_x))\n'
            'print("class (0/1):", predict_class(sample_x))\n'
        )
    return (
        f"FEATURE_ORDER = {order_json}\n"
        f"WEIGHTS = {w_json}\n"
        f"INTERCEPT = {b_json}\n\n"
        "def predict(x):\n"
        "    return sum(w * xi for w, xi in zip(WEIGHTS, x)) + INTERCEPT\n\n"
        "# Replace sample_x with your values in the same order as FEATURE_ORDER\n"
        "sample_x = [0.0] * len(FEATURE_ORDER)\n"
        'print("prediction:", predict(sample_x))\n'
    )


# =========================
# START TRAINING
# =========================
@ml_bp.route("/api/ml/start-training", methods=["POST"])
def start_training():
    try:
        user_id = request.form.get("user_id")
        model_type = request.form.get("model_type", "linear_regression")
        validation_type = request.form.get("validation_type", "holdout")
        split_mode = request.form.get("split_mode", "iid")

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        # =========================
        # FILE HANDLING
        # =========================
        file = request.files.get("file")
        dataset_url = None

        if file:
            filename = file.filename.replace(" ", "_")
            unique_name = f"{uuid.uuid4().hex}_{filename}"
            if not supabase:
                return jsonify({
                    "error": "Supabase storage is required for distributed ML. Configure SUPABASE_URL and SUPABASE_KEY."
                }), 500
            try:
                file_bytes = file.read()
                path = f"user_{user_id}/{unique_name}"

                supabase.storage.from_("datasets").upload(path, file_bytes)
                dataset_url = supabase.storage.from_("datasets").get_public_url(path)

            except Exception as e:
                return jsonify({"error": f"Supabase upload failed: {str(e)}"}), 500

        else:
            # fallback (if frontend sends URL instead)
            dataset_url = request.form.get("dataset_url")

        if not dataset_url:
            return jsonify({"error": "Dataset file or URL is required"}), 400

        # =========================
        # SPAWN ML JOB
        # =========================
        result = spawn_distributed_ml(
            user_id,
            dataset_url,
            model_type,
            validation_type,
            split_mode=split_mode,
        )

        # ensure frontend never gets undefined
        if "error" in result:
            return jsonify(result), 400

        return jsonify({
            "status": "success",
            "task_id": result.get("job_id"),
            "job_id": result.get("job_id"),
            "message": result.get("message")
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "error": "Internal server error",
            "details": str(e)
        }), 500


# =========================
# RECEIVE AGENT RESULT
# =========================
@ml_bp.route("/agent/tasks/<int:task_id>/result", methods=["POST"])
def receive_ml_result(task_id):
    try:
        data = request.json

        if not data:
            return jsonify({"error": "No JSON data received"}), 400

        child_task = TaskRequest.query.get(task_id)

        if not child_task:
            return jsonify({"error": "Task not found"}), 404

        # STATUS UPDATE
        child_task.status = "completed"

        # METRICS NORMALIZATION
        metrics = data.get("metrics") or {}

        if not metrics:
            metrics = {
                "accuracy": data.get("accuracy", 0),
                "precision": data.get("precision", 0),
                "recall": data.get("recall", 0),
                "f1_score": data.get("f1_score") or data.get("f1", 0)
            }

        # STORE RESULT
        child_task.message = json.dumps({
            "weights": data.get("weights"),
            "intercept": data.get("intercept"),
            "metrics": {
                "accuracy": float(metrics.get("accuracy", 0)),
                "precision": float(metrics.get("precision", 0)),
                "recall": float(metrics.get("recall", 0)),
                "f1_score": float(metrics.get("f1_score", 0))
            }
        })

        # Log task completion
        log_event(child_task.id, child_task.assigned_node_id, "completed", 
                 f"ML Task completed: accuracy={metrics.get('accuracy', 0):.3f}, f1={metrics.get('f1_score', 0):.3f}")

        db.session.commit()

        # TRIGGER AGGREGATION
        if child_task.parent_task_id:
            aggregate_ml_results(child_task.parent_task_id)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "error": "Failed to process result",
            "details": str(e)
        }), 500


# =========================
# GET JOB STATUS
# =========================
@ml_bp.route("/api/ml/status/<int:job_id>", methods=["GET"])
def get_ml_status(job_id):
    try:
        parent = TaskRequest.query.get(job_id)

        if not parent:
            return jsonify({"error": "Job not found"}), 404

        # safe JSON parsing
        try:
            parent_meta = json.loads(parent.message) if parent.message else {}
        except (json.JSONDecodeError, TypeError):
            parent_meta = {}

        current_round = parent_meta.get("round", 1)

        children = TaskRequest.query.filter_by(parent_task_id=job_id).all()
        completed_count = len([c for c in children if c.status == "completed"])
        total_count = len(children)

        # progress logic
        base_progress = 50 if current_round == 2 else 0
        round_progress = (completed_count / total_count * 50) if total_count > 0 else 0
        total_progress = base_progress + round_progress

        response = {
            "job_id": job_id,
            "status": parent.status,
            "current_round": current_round,
            "progress": round(total_progress, 2),
            "node_status": f"{completed_count}/{total_count} nodes finished",
            "model_ready": parent.status == "completed"
        }

        # FINAL MODEL RESPONSE
        if parent.status == "completed":
            res = MLResult.query.filter_by(task_id=job_id).first()

            if res:
                try:
                    model_artifact = json.loads(res.feature_importance)
                except (json.JSONDecodeError, TypeError):
                    model_artifact = {}

                #the actual model
                response["model_file_content"] = model_artifact

                #The Data Metrics (kept as FLOAT values for the download)
                response["metrics"] = {
                    "accuracy": res.accuracy,
                    "precision": res.precision,
                    "recall": res.recall,
                    "f1_score": res.f1_score
                    }
                
                #The UI Metrics (formatted as STRINGS for the frontend display)
                response["display_metrics"] = {
                    "accuracy": f"{round(res.accuracy * 100, 3)}%",
                    "precision": f"{round(res.precision * 100, 3)}%",
                    "recall": f"{round(res.recall * 100, 3)}%",
                    "f1_score": f"{round(res.f1_score * 100, 3)}%"
                }
                
                response["message"] = " Model training complete"

        return jsonify(response), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "error": "Failed to fetch status",
            "details": str(e)
        }), 500


@ml_bp.route("/api/ml/download-model/<int:job_id>", methods=["GET"])
def download_model(job_id):
    try:
        result = MLResult.query.filter_by(task_id=job_id).first()

        if not result:
            return jsonify({"error": "Model not found"}), 404

        model = json.loads(result.feature_importance)

        model_type = model["model_metadata"]["model_type"]
        feature_order = model["model_metadata"].get("feature_columns") or []
        params = model.get("model_parameters", {})
        weights_flat = _flatten_weights(params.get("weights"))
        bias = _scalar_intercept(params.get("intercept"))

        if weights_flat is not None and (
            not feature_order or len(feature_order) != len(weights_flat)
        ):
            feature_order = [
                f"feature_{i + 1}" for i in range(len(weights_flat))
            ]

        # Dynamic formula description based on model type
        if model_type == "logistic_regression":
            formula = "z = sum(w_i * x_i) + b; y = 1 / (1 + exp(-z))"
        else:
            formula = "y = sum(w_i * x_i) + b"

        python_snippet = None
        if weights_flat is not None and bias is not None:
            python_snippet = _local_python_snippet(
                model_type, weights_flat, bias, feature_order
            )

        final_file = {
            "README": {
                "description": "Federated ML Model (2-Round FedAvg)",
                "model_type": model_type,
                "formula": formula,
                "feature_order": feature_order,
                "note": "For logistic regression, apply threshold 0.5 for classification",
                "input_example": {col: 0 for col in feature_order},
                "how_to_use": {
                    "local_python": "Copy python_snippet into a .py file and run (no API required).",
                    "input_template": {col: 0 for col in feature_order},
                    "input_note": "Provide values in the same order as feature_order.",
                    "payload_format": {
                        "features": {col: 0 for col in feature_order},
                        "threshold": 0.5
                    },
                },
            },
            "model": model,
            "python_snippet_lines": python_snippet.splitlines() if python_snippet else [],
        }

        return Response(
            json.dumps(final_file, indent=2),
            mimetype="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=model_{job_id}.json"
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@ml_bp.route("/api/ml/predict-model/<int:job_id>", methods=["POST"])
def predict_model(job_id):
    # Runtime-safe prediction endpoint using JSON model artifact.
    # Avoids pickle loading risks and cross-version incompatibility.

    try:
        result = MLResult.query.filter_by(task_id=job_id).first()
        if not result:
            return jsonify({"error": "Model not found"}), 404

        artifact = json.loads(result.feature_importance)
        metadata = artifact.get("model_metadata", {})
        params = artifact.get("model_parameters", {})

        feature_columns = metadata.get("feature_columns", [])
        model_type = metadata.get("model_type", "linear_regression")
        weights = params.get("weights")
        intercept = params.get("intercept")

        # Normalize vector shape for backward compatibility:
        # old artifacts may store weights as [[w1, w2, ...]].
        if isinstance(weights, list) and len(weights) == 1 and isinstance(weights[0], list):
            weights = weights[0]

        if not feature_columns or weights is None or intercept is None:
            return jsonify({"error": "Model artifact is incomplete"}), 400

        body = request.get_json(silent=True) or {}
        provided = body.get("features")
        threshold = float(body.get("threshold", 0.5))

        if provided is None:
            return jsonify({"error": "features is required"}), 400

        if isinstance(provided, dict):
            vector = [float(provided.get(col, 0)) for col in feature_columns]
        elif isinstance(provided, list):
            if len(provided) != len(feature_columns):
                return jsonify({
                    "error": f"features length must be {len(feature_columns)} to match model feature_columns"
                }), 400
            vector = [float(v) for v in provided]
        else:
            return jsonify({"error": "features must be either object or array"}), 400

        bias = intercept[0] if isinstance(intercept, list) else intercept
        z = sum(float(w) * x for w, x in zip(weights, vector)) + float(bias)

        if model_type == "logistic_regression":
            probability = 1.0 / (1.0 + math.exp(-z))
            prediction = 1 if probability >= threshold else 0
            return jsonify({
                "job_id": job_id,
                "model_type": model_type,
                "feature_columns": feature_columns,
                "prediction": prediction,
                "probability": probability,
                "threshold": threshold
            }), 200

        return jsonify({
            "job_id": job_id,
            "model_type": model_type,
            "feature_columns": feature_columns,
            "prediction": z
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
