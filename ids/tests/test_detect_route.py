"""routes/detect.py - now also computes and returns ids.features.extract_features() on every
call (see that module's and detect.py's own comments for why). These exercise the actual Flask
route via its test client, not the feature functions directly (test_features.py already covers
those in isolation) - the thing worth locking in here is that every response branch (blacklisted,
SQL, XSS, benign/model) includes the same "features" key, and that wiring it in didn't change any
existing "prediction" behavior.

No SUPABASE_URL is set in this test environment (ids/tests/conftest.py doesn't set one), so
is_blacklisted()/count_attacks() fail fast with requests' own MissingSchema error on a relative
URL and degrade to their documented safe defaults - no real network call, no mocking needed for
those. log_intrusion() is monkeypatched out in the SQL/XSS tests specifically because it calls a
real third-party geolocation API (ids/ids/geo_lookup.py) before ever touching Supabase, which a
unit test must not do regardless of how gracefully it would eventually fail.
"""
import sys
import types

# Same stub as test_model_loader.py: app.py -> routes.detect -> ids.detector -> ids.model_loader
# would otherwise attempt a real network download of the model file at import time.
_fake_gdown = types.ModuleType("gdown")
_fake_gdown.download = lambda *a, **kw: None
sys.modules.setdefault("gdown", _fake_gdown)

from app import app as flask_app  # noqa: E402


def _client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_benign_login_returns_prediction_and_features():
    r = _client().post("/detect", json={"email": "student@uog.edu.pk", "password": "hunter2"})
    assert r.status_code == 200
    body = r.get_json()
    assert "prediction" in body
    assert "features" in body
    assert set(body["features"].keys()) == {
        "payload_length", "unusual_character_ratio", "has_sql_pattern", "has_xss_pattern",
        "login_attempts_per_minute", "is_new_device",
    }


def test_sql_injection_response_includes_features(monkeypatch):
    import routes.detect as detect_route
    monkeypatch.setattr(detect_route, "log_intrusion", lambda *a, **kw: None)

    r = _client().post("/detect", json={"email": "' OR 1=1 --", "password": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["prediction"] == "SQL Injection Attempt"
    assert body["features"]["has_sql_pattern"] == 1


def test_xss_response_includes_features(monkeypatch):
    import routes.detect as detect_route
    monkeypatch.setattr(detect_route, "log_intrusion", lambda *a, **kw: None)

    r = _client().post("/detect", json={"email": "<script>alert(1)</script>", "password": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["prediction"] == "XSS Attempt"
    assert body["features"]["has_xss_pattern"] == 1


def test_features_are_computed_before_the_blacklist_short_circuit(monkeypatch):
    """login_attempts_per_minute/is_new_device must see every call, including ones about to be
    blocked - otherwise a blacklisted IP hammering the endpoint wouldn't show up in its own rate
    feature at all."""
    import routes.detect as detect_route
    monkeypatch.setattr(detect_route, "is_blacklisted", lambda ip: True)

    r = _client().post("/detect", json={"email": "s@uog.edu.pk", "password": "x"})
    body = r.get_json()
    assert body["prediction"] == "Blocked: IP Blacklisted"
    assert "features" in body
    assert body["features"]["payload_length"] > 0
