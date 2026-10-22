import os
import requests
from ids.geo_lookup import get_geo_location
from ids.email_alert import send_email_alert
from datetime import datetime, timezone

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS

def log_intrusion(email, attack_type, ip_address, explanation=None):
    """explanation: the top-3 SHAP feature contributions from ids.detector.explain(), only ever
    non-None for the ML branch of routes/detect.py - the SQL/XSS/blacklist branches call this
    with no explanation (their own attack_type label already names the specific rule that
    matched, which is its own explanation). Stored alongside the log entry (not just returned
    from /detect) so the Security Dashboard can show "every alert shows the three features that
    most influenced the decision" (Component 9, Objective 6) for past alerts too, not only the
    one live response that happened to trigger them."""
    geo = get_geo_location(ip_address)

    # Safe email sending
    try:
        send_email_alert(
            email,
            attack_type,
            ip_address,
            geo["country"],
            geo["city"]
        )
    except Exception as e:
        print("Email alert failed:", str(e))

    # Save intrusion log to Supabase
    url = f"{SUPABASE_URL}/rest/v1/intrusion_logs"

    headers = HEADERS

    data = {
        "email": email,
        "attack_type": attack_type,
        "ip_address": ip_address,
        "country": geo["country"],
        "city": geo["city"],
        "explanation": explanation,
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=20)
        print(response.status_code, response.text)
    except Exception as e:
        print("Intrusion log save failed:", type(e).__name__, e)