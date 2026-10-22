"""R4's remote-access gateway (app.guacamole_gateway) - see
docs/handoff/R4_sessions_gateway_plan.md item 2f.

The crypto in _encrypt_json_auth is cross-verified against real `openssl` commands run as a
subprocess here, reproducing Apache's own reference script
(extensions/guacamole-auth-json/doc/encrypt-json.sh) byte for byte, rather than just asserting
against another Python implementation of the same misunderstanding. This is the one piece of R4
actually testable without Docker or a running Guacamole instance - everything past "does this
token have the right bytes" (whether Guacamole itself accepts it, whether the SSH connection it
describes actually works) needs the untested-here prototype in guacamole/.
"""
import datetime
import hashlib
import hmac
import json
import shutil
import subprocess

import pytest

from app.guacamole_gateway import (
    GUACAMOLE_PROTOTYPE_TARGET_HOST,
    GuacamoleTokenError,
    WORKSPACE_SSH_USER,
    _encrypt_json_auth,
    issue_ssh_connection_token,
)

TEST_KEY_HEX = "4c0b569e4c96df157eee1b65dd0e4d41"  # the exact example key from Guacamole's own docs

HAVE_OPENSSL = shutil.which("openssl") is not None


def _openssl_reference_token(key_hex, payload):
    """Reproduces extensions/guacamole-auth-json/doc/encrypt-json.sh exactly: HMAC-SHA256 sign
    (binary digest), prepend that signature to the plaintext, AES-128-CBC encrypt (hex key, a
    null 16-byte IV, no salt), base64 the result."""
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    sign = subprocess.run(
        ["openssl", "dgst", "-sha256", "-mac", "HMAC", "-macopt", f"hexkey:{key_hex}", "-binary"],
        input=plaintext, capture_output=True, check=True,
    )
    signature = sign.stdout
    combined = signature + plaintext

    null_iv = "00" * 16
    enc = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", key_hex, "-iv", null_iv, "-nosalt", "-a"],
        input=combined, capture_output=True, check=True,
    )
    # openssl's "-a" wraps base64 output at 64 chars (CRLF on this Windows build) by default -
    # strip ALL whitespace, not just "\n", before comparing to our own unwrapped output.
    return "".join(enc.stdout.decode("ascii").split())


@pytest.mark.skipif(not HAVE_OPENSSL, reason="openssl not available on PATH")
def test_token_format_matches_the_real_openssl_reference_script():
    payload = {"username": "arbitraryUsername", "expires": 1446323765000,
               "connections": {"conn": {"protocol": "ssh",
                                       "parameters": {"hostname": "127.0.0.1", "port": "2222",
                                                      "username": "abc", "password": "hunter2"}}}}
    ours = _encrypt_json_auth(TEST_KEY_HEX, payload)
    reference = _openssl_reference_token(TEST_KEY_HEX, payload)
    assert ours == reference


@pytest.mark.skipif(not HAVE_OPENSSL, reason="openssl not available on PATH")
def test_token_decrypts_back_to_the_exact_payload_via_openssl():
    """The inverse direction: decrypt our own token with openssl, strip the 32-byte HMAC
    prefix, and confirm what's left is exactly the JSON we meant to send, and that the
    signature verifies against the same key."""
    payload = {"username": "someone", "expires": 9999999999999, "connections": {}}
    token = _encrypt_json_auth(TEST_KEY_HEX, payload)

    dec = subprocess.run(
        # -A: our token is one unwrapped base64 line, not openssl's own 64-char-wrapped output -
        # without it, openssl's base64 decoder mishandles the line length and corrupts the block.
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", TEST_KEY_HEX, "-iv", "00" * 16,
         "-nosalt", "-a", "-A"],
        input=token.encode("ascii"), capture_output=True, check=True,
    )
    combined = dec.stdout
    signature, plaintext = combined[:32], combined[32:]
    assert json.loads(plaintext) == payload
    expected_sig = hmac.new(bytes.fromhex(TEST_KEY_HEX), plaintext, hashlib.sha256).digest()
    assert signature == expected_sig


def test_encrypt_rejects_a_key_that_is_not_32_hex_digits():
    with pytest.raises(GuacamoleTokenError, match="not valid hex"):
        _encrypt_json_auth("not-hex", {"a": 1})
    with pytest.raises(GuacamoleTokenError, match="32 hex digits"):
        _encrypt_json_auth("ab" * 8, {"a": 1})  # 8 bytes, too short


# ---------------------------------------------------------------------------------------------
# issue_ssh_connection_token: every reason it should refuse, and the one shape it should accept
# ---------------------------------------------------------------------------------------------

def _running_remote_task(make_user, db):
    from app.models import TaskRequest

    student = make_user("ssh_student", "ssh_student@uog.edu.pk")
    task = TaskRequest(
        user_id=student.id, mode="remote", status="running",
        ssh_port=2222, vm_password="session-secret-pw",
        expiry_time=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30),
    )
    db.session.add(task)
    db.session.commit()
    return student, task


def test_issues_a_valid_token_for_the_sessions_own_owner(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student, task = _running_remote_task(make_user, db)
    token = issue_ssh_connection_token(task, student)
    assert isinstance(token, str) and token

    if HAVE_OPENSSL:
        dec = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-K", TEST_KEY_HEX, "-iv", "00" * 16,
             "-nosalt", "-a", "-A"],
            input=token.encode("ascii"), capture_output=True, check=True,
        )
        payload = json.loads(dec.stdout[32:])
        assert payload["username"] == "ssh_student"
        conn = payload["connections"][f"workspace-{task.id}"]
        assert conn["protocol"] == "ssh"
        assert conn["parameters"]["port"] == "2222"
        assert conn["parameters"]["password"] == "session-secret-pw"
        assert conn["parameters"]["username"] == WORKSPACE_SSH_USER
        assert conn["parameters"]["hostname"] == GUACAMOLE_PROTOTYPE_TARGET_HOST


def test_refuses_without_a_configured_secret_key(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", "")
    student, task = _running_remote_task(make_user, db)
    with pytest.raises(GuacamoleTokenError, match="not configured"):
        issue_ssh_connection_token(task, student)


def test_refuses_a_session_that_belongs_to_someone_else(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    _, task = _running_remote_task(make_user, db)
    someone_else = make_user("someone_else", "someone_else@uog.edu.pk")
    with pytest.raises(GuacamoleTokenError, match="does not belong to you"):
        issue_ssh_connection_token(task, someone_else)


def test_refuses_a_physical_mode_session(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    from app.models import TaskRequest
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student = make_user("physical_student", "physical_student@uog.edu.pk")
    task = TaskRequest(user_id=student.id, mode="physical", status="running",
                       expiry_time=datetime.datetime.now(datetime.timezone.utc)
                       + datetime.timedelta(minutes=30))
    db.session.add(task)
    db.session.commit()
    with pytest.raises(GuacamoleTokenError, match="only workspace sessions"):
        issue_ssh_connection_token(task, student)


def test_refuses_a_session_that_is_not_running(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student, task = _running_remote_task(make_user, db)
    task.status = "pending"
    db.session.commit()
    with pytest.raises(GuacamoleTokenError, match="not running"):
        issue_ssh_connection_token(task, student)


def test_refuses_a_session_with_no_ssh_target_yet(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student, task = _running_remote_task(make_user, db)
    task.ssh_port = None
    db.session.commit()
    with pytest.raises(GuacamoleTokenError, match="no SSH target"):
        issue_ssh_connection_token(task, student)


def test_refuses_an_expired_session(make_user, db, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student, task = _running_remote_task(make_user, db)
    task.expiry_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    db.session.commit()
    with pytest.raises(GuacamoleTokenError, match="already expired"):
        issue_ssh_connection_token(task, student)


# ---------------------------------------------------------------------------------------------
# The route: GET /tasks/<task_id>/guacamole-token
# ---------------------------------------------------------------------------------------------

def test_route_returns_a_token_for_the_owning_student(client, make_user, db, auth, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student, task = _running_remote_task(make_user, db)
    r = client.get(f"/tasks/{task.id}/guacamole-token", headers=auth(student))
    assert r.status_code == 200
    assert r.get_json()["token"]


def test_route_refuses_another_students_session(client, make_user, db, auth, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    _, task = _running_remote_task(make_user, db)
    someone_else = make_user("other_student", "other_student@uog.edu.pk")
    r = client.get(f"/tasks/{task.id}/guacamole-token", headers=auth(someone_else))
    assert r.status_code == 403


def test_route_returns_400_for_a_session_not_running(client, make_user, db, auth, monkeypatch):
    import app.guacamole_gateway as gw
    monkeypatch.setattr(gw, "GUACAMOLE_JSON_SECRET_KEY", TEST_KEY_HEX)
    student, task = _running_remote_task(make_user, db)
    task.status = "completed"
    db.session.commit()
    r = client.get(f"/tasks/{task.id}/guacamole-token", headers=auth(student))
    assert r.status_code == 400
