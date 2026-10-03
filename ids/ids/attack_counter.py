import os
import requests

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS

headers = HEADERS

def count_attacks(ip_address):
    url = f"{SUPABASE_URL}/rest/v1/intrusion_logs?ip_address=eq.{ip_address}"

    try:
        response = requests.get(url, headers=headers, timeout=20)
        data = response.json()
        return len(data) if response.status_code == 200 else 0
    except Exception as e:
        print("Attack count failed:", type(e).__name__, e)
        return 0