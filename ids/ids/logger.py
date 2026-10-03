import os
import requests
from ids.geo_lookup import get_geo_location
from ids.email_alert import send_email_alert
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def log_intrusion(email, attack_type, ip_address):
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

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "email": email,
        "attack_type": attack_type,
        "ip_address": ip_address,
        "country": geo["country"],
        "city": geo["city"],
    }

    response = requests.post(url, json=data, headers=headers)

    print(response.status_code, response.text)