from flask import Blueprint, request, jsonify
from ids.detector import predict_attack
from ids.features import extract_features
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

    # R2: request-level features (see ids/ids/features.py's docstring for the design choices
    # behind each one). Computed for every call, including ones about to be blocked below - an
    # attempt is an attempt regardless of outcome, and login_attempts_per_minute/is_new_device
    # need to see every request to mean anything. Fed directly into the model below (see
    # docs/handoff/R2_explainable_ids_plan.md item 2f for how that model was trained and
    # validated). Also returned in the response so the values are visible and usable without a
    # DB change.
    features = extract_features(email, password, ip_address,
                                user_agent=request.headers.get("User-Agent", ""))

    # BLOCK BLACKLISTED IP FIRST
    if is_blacklisted(ip_address):
        return jsonify({
            "prediction": "Blocked: IP Blacklisted",
            "features": features,
        })

    # SQL Injection Detection
    if detect_sql_injection(email) or detect_sql_injection(password):
        log_intrusion(email, "SQL Injection Attempt", ip_address)

        if count_attacks(ip_address) >= 3:
            blacklist_ip(ip_address)

        return jsonify({
            "prediction": "SQL Injection Attempt",
            "features": features,
        })

    # XSS Detection
    if detect_xss(email) or detect_xss(password):
        log_intrusion(email, "XSS Attempt", ip_address)

        if count_attacks(ip_address) >= 3:
            blacklist_ip(ip_address)

        return jsonify({
            "prediction": "XSS Attempt",
            "features": features,
        })

    # AI IDS Detection
    prediction = predict_attack(features)

    if prediction != "BENIGN":
        log_intrusion(email, prediction, ip_address)

        if count_attacks(ip_address) >= 3:
            blacklist_ip(ip_address)

    return jsonify({
        "prediction": prediction,
        "features": features,
    })