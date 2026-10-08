from flask import Blueprint, request, jsonify, current_app
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from app import db
from app.models import User, Node, NodeMetrics, TaskRequest
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity # NEW
import requests


auth_bp = Blueprint("auth", __name__)

# =====================
# BASIC & HEALTH
# =====================

@auth_bp.route("/")
def index():
    return "HRL-CRMS Backend Running"

@auth_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def _ids_block_reason(email, password):
    """Ask the IDS service to screen a login server-side, so the check can't be
    skipped by calling this API directly instead of going through the website.
    Returns a reason string to block on, or None to let the login proceed
    (including when the IDS is unreachable - it's defense in depth, not the
    only gate: the password is still checked normally either way)."""
    ids_base = current_app.config.get("IDS_BASE")
    if not ids_base:
        return None
    try:
        resp = requests.post(
            f"{ids_base}/detect",
            json={"email": email, "password": password},
            headers={
                "X-Internal-Secret": current_app.config["JWT_SECRET_KEY"],
                "X-Original-IP": request.remote_addr or "",
            },
            timeout=4,
        )
        prediction = (resp.json() or {}).get("prediction", "BENIGN")
    except Exception:
        return None
    if prediction and prediction != "BENIGN":
        return prediction
    return None


# =====================
# AUTH ROUTES
# =====================

@auth_bp.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    block_reason = _ids_block_reason(email, password)
    if block_reason:
        return jsonify({"error": f"Suspicious activity detected: {block_reason}"}), 403

    user = User.query.filter_by(email=email).first()

    if not user or not check_password_hash(user.password, password):
        return jsonify({"error": "invalid credentials"}), 401

    # Create an access token for the user
    # We include the user ID and role in the token identity
    access_token = create_access_token(identity=str(user.id), additional_claims={"role": user.role})

    return jsonify({
        "access_token": access_token, # This is what the frontend must save
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role
        }
    }), 200

@auth_bp.route("/auth/change_password", methods=["POST"])
@jwt_required()
def change_password():
    current_user_id = get_jwt_identity()
    data = request.get_json()
    user = User.query.filter_by(email=data.get("email")).first()
    
    if not user or str(user.id) != current_user_id:
        return jsonify({"error": "Unauthorized"}), 403
    
    if not check_password_hash(user.password, data.get("old_password")):
        return jsonify({"error": "Invalid current password"}), 400
    
    user.password = generate_password_hash(data.get("new_password"))
    db.session.commit()
    return jsonify({"message": "Password changed successfully"})


        
