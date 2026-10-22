"""R4's remote-access gateway: issues Guacamole JSON-auth tokens for a student's own running
workspace session.

Scope of what this actually does today (see docs/handoff/R4_sessions_gateway_plan.md item 2f for
the full writeup before assuming more than this):
- Builds the exact token format `guacamole-auth-json` expects (verified against Apache's own
  reference script, extensions/guacamole-auth-json/doc/encrypt-json.sh, not guessed from prose):
  HMAC-SHA256 sign the plaintext JSON, prepend the raw signature to it, AES-128-CBC encrypt
  (zero IV, PKCS7 padding) with the same 16-byte key configured in Guacamole's own
  guacamole.properties as `json-secret-key`, then base64 the result.
- Grants exactly one SSH connection, into the one workspace container the requesting student's
  own running session already has - never any other student's session, never a session that
  isn't currently running, never past that session's own expiry.

Scope of what this does NOT do, deliberately left for later:
- It does not run Guacamole itself, or prove the relay works end to end - this repo has no
  Docker available to test that (see guacamole/README.md for the untested-here prototype stack).
- It does not solve reaching a lab PC without an inbound port for SSH the way the existing
  Cloudflare Quick Tunnel already does for the workspace's HTTP (code-server) traffic - the
  target host here (GUACAMOLE_PROTOTYPE_TARGET_HOST) assumes guacd can already reach the
  workspace container directly (same machine or same Docker network), which is true for a local
  prototype and not true for a real lab PC behind no inbound ports. That gap is still open.
- It is a separate SSH login (added to the workspace image alongside code-server), not a relay
  of the code-server HTTP session itself - Guacamole relays RDP/VNC/SSH, not arbitrary HTTP apps.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import timezone

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# The shared secret with Guacamole's own guacamole-auth-json extension (configured there as
# json-secret-key, 32 hex digits / 16 bytes) - deliberately its own env var, not derived from
# JWT_SECRET_KEY: it has to be copied byte-for-byte into an external system's config file, so an
# explicit, independently-generated value is less surprising to whoever sets that up than a
# derived one they'd have to reverse-engineer.
GUACAMOLE_JSON_SECRET_KEY = os.environ.get("GUACAMOLE_JSON_SECRET_KEY", "")

# Where guacd should reach the workspace container's SSH port. Correct only when guacd runs on
# the same machine (or same Docker network) as the node agent's workspace containers - true for
# the local prototype in guacamole/, not true for a real lab PC with no inbound ports. Fixing
# that for real multi-PC deployment is separate, still-open work (see this module's docstring).
GUACAMOLE_PROTOTYPE_TARGET_HOST = os.environ.get("GUACAMOLE_PROTOTYPE_TARGET_HOST", "127.0.0.1")

# The user created inside the workspace container for SSH login - see
# node-agent/pool_runtime.py's WORKSPACE_DOCKERFILE, which reuses the LinuxServer.io base
# image's existing PUID/PGID-mapped user rather than creating a new one.
WORKSPACE_SSH_USER = "abc"


class GuacamoleTokenError(Exception):
    """Raised with a message safe to show the requesting student - never includes the secret
    key or any other session's data."""


def _encrypt_json_auth(secret_key_hex, payload):
    """guacamole-auth-json's exact token format - see this module's docstring for the
    reference this was verified against. Raises GuacamoleTokenError if the key isn't a valid
    16-byte hex string, since a malformed key would otherwise fail silently (Guacamole would
    just reject every token with a generic "invalid credentials" error, with nothing in this
    app's own logs pointing at why)."""
    try:
        key = bytes.fromhex(secret_key_hex)
    except ValueError:
        raise GuacamoleTokenError("GUACAMOLE_JSON_SECRET_KEY is not valid hex")
    if len(key) != 16:
        raise GuacamoleTokenError("GUACAMOLE_JSON_SECRET_KEY must be 32 hex digits (16 bytes)")

    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key, plaintext, hashlib.sha256).digest()
    combined = signature + plaintext

    padder = padding.PKCS7(128).padder()
    padded = padder.update(combined) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return base64.b64encode(ciphertext).decode("ascii")


def issue_ssh_connection_token(task, requesting_user):
    """Issues a token granting `requesting_user` exactly one SSH connection, into exactly the
    workspace container `task` already has - raises GuacamoleTokenError (message safe to return
    to the student as-is) for every reason this could be wrong, checked in order from "is this
    even the right session" to "is it actually ready":
    - the session must belong to the requesting user (never another student's session, and
      never trusted from a client-supplied id - callers must pass the user the request's own
      JWT identifies, same as the rest of this codebase's `_owns_task` pattern)
    - it must be a remote (workspace) session - physical sessions have no SSH target
    - it must currently be running, with a live expiry in the future - no token for a session
      that hasn't started yet, already ended, or was never given an SSH target
    """
    if not GUACAMOLE_JSON_SECRET_KEY:
        raise GuacamoleTokenError("the remote-access gateway is not configured on this server")
    if task.user_id != requesting_user.id:
        raise GuacamoleTokenError("this session does not belong to you")
    if task.mode != "remote":
        raise GuacamoleTokenError("only workspace sessions have an SSH connection")
    if task.status != "running":
        raise GuacamoleTokenError(f"session is not running (status: {task.status})")
    if not task.ssh_port or not task.vm_password:
        raise GuacamoleTokenError("this session's workspace has no SSH target yet")
    if not task.expiry_time:
        raise GuacamoleTokenError("session has no expiry set")

    # SQLite drops tzinfo on round-trip (see app.allocation_engine's own comment on the same
    # issue) - every expiry_time this app ever writes is UTC, so a naive value read back is UTC
    # too, never local time. Without this, .timestamp() below would interpret it as local time
    # instead, silently shifting it by the server's UTC offset.
    expiry = task.expiry_time
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    expires_ms = int(expiry.timestamp() * 1000)
    if expires_ms <= int(time.time() * 1000):
        raise GuacamoleTokenError("session has already expired")

    payload = {
        "username": requesting_user.username,
        "expires": expires_ms,
        "connections": {
            f"workspace-{task.id}": {
                "protocol": "ssh",
                "parameters": {
                    "hostname": GUACAMOLE_PROTOTYPE_TARGET_HOST,
                    "port": str(task.ssh_port),
                    "username": WORKSPACE_SSH_USER,
                    "password": task.vm_password,
                },
            }
        },
    }
    return _encrypt_json_auth(GUACAMOLE_JSON_SECRET_KEY, payload)
