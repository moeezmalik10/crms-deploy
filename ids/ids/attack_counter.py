import os
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def count_attacks(ip_address):
    url = f"{SUPABASE_URL}/rest/v1/intrusion_logs?ip_address=eq.{ip_address}"

    response = requests.get(url, headers=headers)
    data = response.json()

    return len(data)