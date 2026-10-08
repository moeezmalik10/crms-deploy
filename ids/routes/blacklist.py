import os
import re
from functools import wraps

import jwt
from flask import Blueprint, jsonify, request
import requests

blacklist_bp = Blueprint("blacklist", __name__)

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS, JWT_SECRET_KEY, diagnose

headers = HEADERS

_IP_RE = re.compile(r"^[0-9a-fA-F.:]{3,45}$")  # IPv4/IPv6 literal, nothing else


def _require_admin(fn):
    """Only an admin holding a real backend login token may view or change the blacklist."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not JWT_SECRET_KEY:
            return jsonify({"error": "admin login required"}), 401
        try:
            claims = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
        except Exception:
            return jsonify({"error": "invalid or expired token"}), 401
        if claims.get("role") != "admin":
            return jsonify({"error": "admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# Get all blacklisted IPs
@blacklist_bp.route("/blacklisted", methods=["GET"])
@_require_admin
def get_blacklisted():
    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips?select=*"

    try:
        response = requests.get(url, headers=headers, timeout=20)
        data = response.json()
        if response.status_code == 200:
            return jsonify(data)
    except Exception as e:
        print("Supabase read failed:", type(e).__name__, e)
    # Explain the problem instead of a bare 500 (details also at /health/supabase)
    return jsonify(diagnose()), 502


# Remove blocked IP
@blacklist_bp.route("/unblock", methods=["DELETE"])
@_require_admin
def unblock_ip():
    ip_address = (request.json or {}).get("ip_address", "")

    if not _IP_RE.match(ip_address or ""):
        return jsonify({"error": "invalid ip_address"}), 400

    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips?ip_address=eq.{ip_address}"

    try:
        response = requests.delete(url, headers=headers, timeout=20)
    except Exception as e:
        return jsonify({"error": f"could not reach Supabase: {e}"}), 502

    if response.status_code not in (200, 204):
        return jsonify({"error": f"Supabase refused the delete (HTTP {response.status_code})"}), 502

    return jsonify({
        "message": f"{ip_address} unblocked successfully"
    })