import os
import requests

from ids.settings import SUPABASE_URL, SUPABASE_KEY, HEADERS

headers = HEADERS

def is_blacklisted(ip_address):
    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips?ip_address=eq.{ip_address}"
    try:
        response = requests.get(url, headers=headers, timeout=20)
        data = response.json()
        return response.status_code == 200 and len(data) > 0
    except Exception as e:
        print("Blacklist check failed:", type(e).__name__, e)
        return False


def blacklist_ip(ip_address):
    url = f"{SUPABASE_URL}/rest/v1/blacklisted_ips"

    try:
        requests.post(url, json={
            "ip_address": ip_address
        }, headers=headers, timeout=20)
    except Exception as e:
        print("Blacklist save failed:", type(e).__name__, e)