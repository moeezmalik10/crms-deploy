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

The model now wired into prediction (ids/r2_model.pkl) is the one described in
docs/handoff/R2_explainable_ids_plan.md item 2f: two earlier models were built and found unsafe
on real login-shaped traffic before ever reaching a commit (item 2, then item 2d - the latter
validated at 2.7% FPR against individually-sampled passwords, but turned out to have a 40-60%
FPR against realistically-varied @uog.edu.pk email local-parts once tested live, because the
training simulator's benign email generator was itself too narrow a "shape" - see item 2e). This
model retrains on a fixed, five-shape email generator (dotted names, underscored names, bare
names+numbers, roll-number-style numerics) alongside the same real password corpus - see item 2f
for the full validation story before trusting this comment alone.
"""
from app import app as flask_app


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
        "special_character_count", "max_special_char_run", "url_encoded_count",
        "has_path_traversal", "sql_keyword_count", "digit_ratio",
        "login_attempts_per_minute", "is_new_device",
    }
    # A BENIGN prediction has nothing alert-worthy to explain - see ids/ids/explainer.py's
    # own docstring for why SHAP is only ever computed for the Anomalous branch.
    assert body["explanation"] is None


def test_sql_injection_response_includes_features(monkeypatch):
    import routes.detect as detect_route
    monkeypatch.setattr(detect_route, "log_intrusion", lambda *a, **kw: None)

    r = _client().post("/detect", json={"email": "' OR 1=1 --", "password": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["prediction"] == "SQL Injection Attempt"
    assert body["features"]["has_sql_pattern"] == 1
    assert body["explanation"] is None  # a deterministic rule match, not the ML branch


def test_xss_response_includes_features(monkeypatch):
    import routes.detect as detect_route
    monkeypatch.setattr(detect_route, "log_intrusion", lambda *a, **kw: None)

    r = _client().post("/detect", json={"email": "<script>alert(1)</script>", "password": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["prediction"] == "XSS Attempt"
    assert body["features"]["has_xss_pattern"] == 1
    assert body["explanation"] is None  # a deterministic rule match, not the ML branch


def test_realistic_benign_logins_are_not_flagged_by_the_model():
    """The regression test for the exact failure that sank the first shipping attempt: a
    CSIC-trained model flagged ~100% of ordinary logins as anomalous. Each login here is given
    its own source IP via environ_overrides - routes.detect's login_attempts_per_minute is a
    process-global, per-IP counter (correctly, in production: that's what lets it notice one
    source hammering the endpoint), so without this, five *different users'* first-ever logins
    sent through the same test client (which defaults every request to 127.0.0.1) look
    indistinguishable from a single attacker's credential-stuffing burst - exactly the shared-
    state artifact this project already hit once before at the model-validation layer."""
    logins = [
        ("student1@uog.edu.pk", "Pakistan@123"),
        ("ali.khan@uog.edu.pk", "Sunshine99!"),
        ("fatima.noor@uog.edu.pk", "MyP@ssw0rd"),
        ("usman.tariq@uog.edu.pk", "football2024"),
        ("zara.ahmed@uog.edu.pk", "Qwerty12345"),
    ]
    client = _client()
    flagged = []
    for i, (email, password) in enumerate(logins):
        r = client.post("/detect", json={"email": email, "password": password},
                        environ_overrides={"REMOTE_ADDR": f"203.0.113.{i + 1}"})
        body = r.get_json()
        if body["prediction"] != "BENIGN":
            flagged.append((email, body["prediction"]))
    assert not flagged, f"model flagged realistic benign logins as attacks: {flagged}"


def test_varied_realistic_uog_email_shapes_are_mostly_not_flagged():
    """The regression test for item 2e's failure: a model validated fine on password diversity
    turned out to have a 40-60% false-positive rate once @uog.edu.pk email *local-part* shape was
    varied realistically (dotted names, underscored names, roll-number-style numerics) - accounts
    here are admin-created (backend/app/admin_routes.py's create_user()), so no one shape is
    guaranteed. This exercises all five shapes training.simulate_attacks._student_email() now
    generates, plus two shapes deliberately NOT in that generator (a three-part dotted name, a
    plain short username with no numbers) to check this isn't just memorizing its own five molds.
    A handful of misses among OOD shapes is acceptable (no training data is perfectly
    generalizing); a high rate across the board is the item 2e failure recurring."""
    logins = [
        ("hamza.chaudhry@uog.edu.pk", "Pakistan123!"),
        ("sana_khan77@uog.edu.pk", "MyP@ssw0rd2024"),
        ("ayesha458@uog.edu.pk", "football99"),
        ("23012323192@uog.edu.pk", "Sunshine88"),
        ("moeez.qureshi26@uog.edu.pk", "Qwerty!2345"),
        ("ali.bilal.khan@uog.edu.pk", "StudyHard21"),   # OOD: three-part dotted name
        ("noman@uog.edu.pk", "Gujrat@786"),             # OOD: bare username, no number
    ]
    client = _client()
    flagged = []
    for i, (email, password) in enumerate(logins):
        r = client.post("/detect", json={"email": email, "password": password},
                        environ_overrides={"REMOTE_ADDR": f"198.51.100.{i + 1}"})
        body = r.get_json()
        if body["prediction"] != "BENIGN":
            flagged.append((email, body["prediction"]))
    assert len(flagged) <= 1, f"model flagged too many realistically-shaped logins: {flagged}"


def test_a_novel_sql_injection_shaped_password_is_flagged(monkeypatch):
    """A payload that trips neither the deterministic SQL/XSS regexes nor looks like a password
    at all should still reach the model and get flagged - this is what the ML layer is for."""
    import routes.detect as detect_route
    monkeypatch.setattr(detect_route, "log_intrusion", lambda *a, **kw: None)
    monkeypatch.setattr(detect_route, "detect_sql_injection", lambda *a, **kw: False)
    monkeypatch.setattr(detect_route, "detect_xss", lambda *a, **kw: False)

    r = _client().post("/detect", json={
        "email": "attacker@evil.com",
        "password": "1' AND SLEEP(5)-- -- UNION SELECT password FROM users WHERE 1=1#",
    })
    body = r.get_json()
    assert body["prediction"] == "Anomalous Request (ML)"
    # "every alert shows the three features that most influenced the decision" (Component 9,
    # Objective 6) - the one case where an explanation should actually be present.
    explanation = body["explanation"]
    assert 0 < len(explanation) <= 3
    for entry in explanation:
        assert set(entry.keys()) == {"feature", "contribution", "value"}
        assert entry["feature"] in body["features"]


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
    assert body["explanation"] is None
