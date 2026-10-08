"""Security helpers for the resource pool.

* Device keys: every contributed device gets a random key when it joins. Only its SHA-256
  is stored. Agents send the key in the X-Device-Key header on every request.
* Join codes: one-time, expire after 30 minutes, also stored only as a hash.
* Storage encryption: each pooled file gets its own AES-256-GCM key. Chunks are encrypted
  before they are sent to contributors, so a contributor cannot read what it stores.
  The file key itself is wrapped (encrypted) with the server master key.
"""
import base64
import hashlib
import os
import secrets
from functools import wraps

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import g, jsonify, request

from app.models import Node

LEGACY_AGENTS_ALLOWED = os.environ.get("ALLOW_LEGACY_AGENTS", "true").lower() != "false"


def sha256_hex(value):
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def new_device_key():
    return "dk_" + secrets.token_urlsafe(32)


def new_join_code():
    # 10 easy-to-type characters, e.g. 7KQ4-M2XP-9C
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def client_public_ip():
    """The device's public IP as seen by the server. Behind Render's proxies the original
    client address is the first entry of X-Forwarded-For (used for display only)."""
    xff = request.headers.get("X-Forwarded-For", "")
    first = xff.split(",")[0].strip() if xff else ""
    return first or (request.remote_addr or "").strip()


def node_from_key():
    key = request.headers.get("X-Device-Key", "").strip()
    if not key:
        return None
    return Node.query.filter_by(device_key_hash=sha256_hex(key)).first()


def agent_auth(name_param=None):
    """Identify the calling agent.

    Keyed devices must present their key. Agents without a key ("legacy", e.g. the lab PC
    agent from before the pool or a browser device) are accepted only while
    ALLOW_LEGACY_AGENTS is not "false", and never for a device that already has a key.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            g.node = node_from_key()
            if request.headers.get("X-Device-Key") and not g.node:
                return jsonify({"error": "invalid or revoked device key"}), 401
            g.legacy = g.node is None
            if g.legacy:
                if not LEGACY_AGENTS_ALLOWED:
                    return jsonify({"error": "device key required - join the pool from the website"}), 401
                claimed = kwargs.get(name_param) if name_param else None
                if claimed:
                    n = Node.query.filter_by(name=claimed).first()
                    if n and n.device_key_hash:
                        return jsonify({"error": "this device is protected by a device key"}), 401
            elif name_param and kwargs.get(name_param) and kwargs.get(name_param) != g.node.name:
                return jsonify({"error": "device key does not belong to this device"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def task_belongs_to_caller(task):
    """Only the agent the task is actually assigned to may report on it.

    Keyed agents are identified by their device key. Legacy (keyless) agents
    carry no such proof, so they must name the PC they claim to be (field
    "pc_name" in the JSON body) and that name must match task.assigned_pc -
    and must not belong to a device that has since taken a device key,
    otherwise a keyed device could be impersonated simply by omitting the key.
    """
    node = getattr(g, "node", None)
    if node is not None:
        return task.assigned_node_id == node.id
    if not LEGACY_AGENTS_ALLOWED:
        return False
    data = request.get_json(silent=True) or {}
    claimed = (data.get("pc_name") or request.args.get("pc_name") or "").strip()
    if not claimed or not task.assigned_pc or claimed != task.assigned_pc:
        return False
    named = Node.query.filter_by(name=claimed).first()
    return not (named and named.device_key_hash)


# ---------------- encryption ----------------
def _master_key():
    secret = os.environ.get("POOL_MASTER_KEY") or os.environ.get("JWT_SECRET_KEY") or ""
    return hashlib.sha256(("crms-pool:" + secret).encode()).digest()


def new_file_key():
    return AESGCM.generate_key(bit_length=256)


def wrap_key(file_key):
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(_master_key()).encrypt(nonce, file_key, b"crms-file-key")).decode()


def unwrap_key(wrapped):
    raw = base64.b64decode(wrapped)
    return AESGCM(_master_key()).decrypt(raw[:12], raw[12:], b"crms-file-key")


def encrypt_chunk(file_key, data, aad):
    nonce = os.urandom(12)
    return nonce + AESGCM(file_key).encrypt(nonce, data, aad)


def decrypt_chunk(file_key, blob, aad):
    return AESGCM(file_key).decrypt(blob[:12], blob[12:], aad)
