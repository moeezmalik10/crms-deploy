from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta    
from werkzeug.security import generate_password_hash, check_password_hash
from app import db
from app.models import User, Node, NodeMetrics, TaskRequest
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity # NEW


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

# =====================
# AUTH ROUTES 
# =====================

@auth_bp.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    user = User.query.filter_by(email=data.get("email")).first()

    if not user or not check_password_hash(user.password, data.get("password")):
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


        
