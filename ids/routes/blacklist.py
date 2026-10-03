import os
from flask import Blueprint, jsonify, request
import requests

blacklist_bp = Blueprint("blacklist", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# Get all blacklisted IPs
@blacklist_bp.route("/blacklisted", methods=["GET"])
def get_blacklisted():
    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips?select=*"

    response = requests.get(url, headers=headers)
    return jsonify(response.json())


# Remove blocked IP
@blacklist_bp.route("/unblock", methods=["DELETE"])
def unblock_ip():
    ip_address = request.json.get("ip_address")

    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips?ip_address=eq.{ip_address}"

    response = requests.delete(url, headers=headers)

    return jsonify({
        "message": f"{ip_address} unblocked successfully"
    })