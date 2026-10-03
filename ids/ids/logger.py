import os
import requests
from ids.geo_lookup import get_geo_location
from ids.email_alert import send_email_alert
from datetime import datetime, timezone

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS

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

    headers = HEADERS

    data = {
        "email": email,
        "attack_type": attack_type,
        "ip_address": ip_address,
        "country": geo["country"],
        "city": geo["city"],
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=20)
        print(response.status_code, response.text)
    except Exception as e:
        print("Intrusion log save failed:", type(e).__name__, e)