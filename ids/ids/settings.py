"""Supabase settings for the IDS, read once from the environment (Render -> crms-ids -> Environment).

Values pasted into a dashboard often pick up quotes, spaces, line breaks or the wrong form of the
address. They are cleaned here so one small paste mistake does not stop the whole IDS.
"""
import base64
import json
import os
import re

import requests


def _clean(value):
    value = (value or "").strip().strip('"').strip("'").strip()
    return re.sub(r"\s+", "", value)


def _clean_url(value):
    url = _clean(value)
    # Dashboard link -> API address: supabase.com/dashboard/project/<ref>  ->  https://<ref>.supabase.co
    m = re.search(r"supabase\.com/dashboard/project/([a-z0-9]+)", url)
    if m:
        return f"https://{m.group(1)}.supabase.co"
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")
    for suffix in ("/rest/v1", "/storage/v1", "/auth/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


SUPABASE_URL = _clean_url(os.environ.get("SUPABASE_URL"))
SUPABASE_KEY = _clean(os.environ.get("SUPABASE_KEY"))
# Shared with the backend's JWT_SECRET_KEY so the IDS can tell an admin's token is real.
JWT_SECRET_KEY = _clean(os.environ.get("JWT_SECRET_KEY"))

HEADERS = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
# Legacy keys (eyJ...) are JWTs and also go in Authorization. New keys (sb_secret_...) must not.
if SUPABASE_KEY.startswith("eyJ"):
    HEADERS["Authorization"] = f"Bearer {SUPABASE_KEY}"


def _key_kind():
    if not SUPABASE_KEY:
        return "missing"
    if SUPABASE_KEY.startswith("sb_secret_"):
        return "secret key (sb_secret_...) - correct"
    if SUPABASE_KEY.startswith("sb_publishable_"):
        return "publishable key - WRONG, use the secret / service_role key"
    if SUPABASE_KEY.startswith("eyJ"):
        try:
            payload = SUPABASE_KEY.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            role = json.loads(base64.urlsafe_b64decode(payload)).get("role", "?")
        except Exception:
            return "not a valid key (could not read it)"
        if role == "service_role":
            return "service_role key - correct"
        return f"{role} key - WRONG, use the service_role key"
    return "not a Supabase key"


def diagnose():
    """What the IDS sees. Never returns the key itself."""
    report = {
        "supabase_url": SUPABASE_URL or "(empty)",
        "supabase_url_raw_was_changed": SUPABASE_URL != (os.environ.get("SUPABASE_URL") or ""),
        "supabase_key": _key_kind(),
        "supabase_key_length": len(SUPABASE_KEY),
    }
    if not SUPABASE_URL or not SUPABASE_KEY:
        report["result"] = "FAIL: SUPABASE_URL or SUPABASE_KEY is empty on crms-ids"
        return report
    if not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co", SUPABASE_URL):
        report["warning"] = "SUPABASE_URL should look like https://<20-letter project ref>.supabase.co"
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/blacklisted_ips?select=id&limit=1", headers=HEADERS, timeout=20)
        report["rest_status"] = r.status_code
        if r.status_code == 200:
            report["result"] = "OK: IDS reads Supabase"
        elif r.status_code in (401, 403):
            report["result"] = "FAIL: Supabase rejected SUPABASE_KEY - paste the service_role / secret key"
        elif r.status_code == 404 or "does not exist" in r.text or "PGRST205" in r.text:
            report["result"] = "FAIL: table blacklisted_ips not found - run database/supabase_setup.sql in Supabase"
        else:
            report["result"] = f"FAIL: Supabase answered {r.status_code}"
        report["rest_reply"] = r.text[:300].strip()
    except Exception as e:
        report["result"] = f"FAIL: cannot reach {SUPABASE_URL} ({type(e).__name__}) - check the project ref in SUPABASE_URL"
        report["error"] = str(e)[:300]
    return report
