import os
from flask import Blueprint, jsonify, request
import requests

blacklist_bp = Blueprint("blacklist", __name__)

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS, diagnose

headers = HEADERS

# Get all blacklisted IPs
@blacklist_bp.route("/blacklisted", methods=["GET"])
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
def unblock_ip():
    ip_address = request.json.get("ip_address")

    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips?ip_address=eq.{ip_address}"

    response = requests.delete(url, headers=headers)

    return jsonify({
        "message": f"{ip_address} unblocked successfully"
    })