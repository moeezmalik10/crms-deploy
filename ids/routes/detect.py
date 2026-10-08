from flask import Blueprint, request, jsonify
from ids.detector import predict_attack
from ids.logger import log_intrusion
from ids.sql_detector import detect_sql_injection
from ids.xss_detector import detect_xss
from ids.blacklist import is_blacklisted, blacklist_ip
from ids.attack_counter import count_attacks
from ids.settings import JWT_SECRET_KEY

detect_bp = Blueprint("detect", __name__)

@detect_bp.route("/detect", methods=["POST"])
def detect():
    data = request.json

    email = data.get("email", "")
    password = data.get("password", "")

    # Real client IP: ProxyFix (see app.py) already resolved this from the proxy's
    # own trusted X-Forwarded-For hop, so a client can no longer spoof it directly.
    # When the backend itself calls us (screening a login server-side), it is the
    # proxy's hop instead of the browser, so it forwards the real client IP - trust
    # that forwarded value only when it's paired with the backend's own secret.
    ip_address = request.remote_addr or ""
    if JWT_SECRET_KEY and request.headers.get("X-Internal-Secret") == JWT_SECRET_KEY:
        forwarded = (request.headers.get("X-Original-IP") or "").strip()
        if forwarded:
            ip_address = forwarded

    # BLOCK BLACKLISTED IP FIRST
    if is_blacklisted(ip_address):
        return jsonify({
            "prediction": "Blocked: IP Blacklisted"
        })

    # SQL Injection Detection
    if detect_sql_injection(email) or detect_sql_injection(password):
        log_intrusion(email, "SQL Injection Attempt", ip_address)

        if count_attacks(ip_address) >= 3:
            blacklist_ip(ip_address)

        return jsonify({
            "prediction": "SQL Injection Attempt"
        })

    # XSS Detection
    if detect_xss(email) or detect_xss(password):
        log_intrusion(email, "XSS Attempt", ip_address)

        if count_attacks(ip_address) >= 3:
            blacklist_ip(ip_address)

        return jsonify({
            "prediction": "XSS Attempt"
        })

    # AI IDS Detection
    prediction = predict_attack(data)

    if prediction != "BENIGN":
        log_intrusion(email, prediction, ip_address)

        if count_attacks(ip_address) >= 3:
            blacklist_ip(ip_address)

    return jsonify({
        "prediction": prediction
    })