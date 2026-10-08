import os
import requests

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS

headers = HEADERS

def count_attacks(ip_address):
    url = f"{SUPABASE_URL}/rest/v1/intrusion_logs"

    try:
        # requests encodes the query params, so ip_address can't inject extra
        # PostgREST filters (e.g. "&limit=0") the way raw string interpolation could.
        response = requests.get(url, headers=headers, params={"ip_address": f"eq.{ip_address}"}, timeout=20)
        data = response.json()
        return len(data) if response.status_code == 200 else 0
    except Exception as e:
        print("Attack count failed:", type(e).__name__, e)
        return 0