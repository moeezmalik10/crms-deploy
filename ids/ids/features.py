"""Request-level features for R2 (explainable, request-level intrusion detection).

The proposal: "The model is trained on features taken from web requests, such as failed logins
per address per minute, request rate, payload length, unusual characters, known injection
patterns and new devices." None of this existed before this file - the current model
(model_loader.py) is a RandomForestClassifier trained on CICIDS2017-style network-flow features
(packet counts, flow duration, destination port...), which detector.py feeds with
request.reindex(columns=features, fill_value=0) against a request body that has none of those
keys - every real feature the current model actually uses is always 0. These functions are the
replacement feature set: computed directly from what /detect actually receives (email, password,
the caller's IP, headers), not flow data that was never available in the first place.

Design decisions made here, each because the proposal's wording doesn't fully specify the
mechanism and a choice had to be made to build anything at all:

- "Failed logins per address per minute" is approximated as *login attempts* per address per
  minute. auth_routes.py calls /detect BEFORE checking the password (see its own comment: this
  screens every attempt, success or failure, so a bypass via calling the backend API directly
  still gets screened) - at the point /detect runs, the real outcome isn't known yet. Attempt
  rate is the signal actually available pre-auth, and is the standard proxy used for
  brute-force/credential-stuffing detection when the true outcome isn't known at inspection
  time. Feeding back real pass/fail after the check would need a new backend->IDS call
  (auth_routes.py would have to report outcomes), which is a real architecture change, not a
  feature-extraction detail - flagged here, not silently built.
- SQL/XSS pattern flags are computed here and exposed as features (for training-time context and
  the SHAP "why" explanation this component still needs to add), but routes/detect.py's existing
  early-return behavior is unchanged: a request that trips the deterministic regex still returns
  immediately and never reaches the ML model. That means these two features will typically read 0
  whenever the model actually runs - by design, not a bug. Making the model evaluate every
  request regardless (removing the early return) would waste inference on requests already
  certain to be attacks; that's a separate tradeoff from feature extraction itself.
- "New device" needs a per-account history of previously-seen device fingerprints, which needs
  something that persists across requests. is_new_device() here uses an in-memory, per-process
  set as its default store - correct for a single Flask process, but it resets on every restart
  and isn't shared across multiple gunicorn workers if this service is ever scaled that way.
  Durable tracking would need a new Supabase table (same pattern as intrusion_logs/
  blacklisted_ips in database/supabase_setup.sql) - not added here; this gives you a fingerprint
  function and a pluggable store (device_store param) so swapping in a real backing store later
  doesn't change the fingerprint logic itself.
"""
import hashlib
import ipaddress
import re
import threading
import time
from collections import defaultdict, deque

from ids.sql_detector import detect_sql_injection
from ids.xss_detector import detect_xss

# Letters, digits, and the characters that routinely show up in a real email/password
# (@ . _ - + for emails; a generous ASCII punctuation set for passwords) are not "unusual".
# Everything else - control characters, most punctuation, non-ASCII - counts toward the ratio.
_ORDINARY_CHARS = re.compile(r"[A-Za-z0-9@._\-+!#$%^&*()\[\]{}:;,'\"/\\ ]")

REQUEST_RATE_WINDOW_SECONDS = 60


def payload_length(email, password):
    """Total characters across both fields - a crafted payload (e.g. a long injection string
    or a stuffed password list entry) tends to be longer than a normal login."""
    return len(email or "") + len(password or "")


def unusual_character_ratio(text):
    """Fraction of characters NOT in the ordinary email/password alphabet. 0.0 for an empty
    string (nothing unusual about nothing), not NaN/division-by-zero."""
    if not text:
        return 0.0
    unusual = sum(1 for c in text if not _ORDINARY_CHARS.match(c))
    return unusual / len(text)


def has_sql_pattern(email, password):
    """1/0 int (not bool) - models and dataframes want a numeric feature column."""
    return int(detect_sql_injection(email or "") or detect_sql_injection(password or ""))


def has_xss_pattern(email, password):
    return int(detect_xss(email or "") or detect_xss(password or ""))


class _SlidingWindowCounter:
    """How many times record() was called for a given key in the last `window_seconds`.
    Thread-safe (Flask/gunicorn can serve requests on multiple threads in one worker); an
    in-memory per-process store, see this module's docstring for what that does and doesn't
    cover."""

    def __init__(self, window_seconds):
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def record(self, key, now=None):
        """Records one hit for `key` now, and returns the count within the window (including
        this hit) - the rate feature for THIS request, not yesterday's count plus one later."""
        now = now if now is not None else time.time()
        with self._lock:
            dq = self._hits[key]
            dq.append(now)
            cutoff = now - self.window_seconds
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq)


_login_attempts = _SlidingWindowCounter(REQUEST_RATE_WINDOW_SECONDS)


def login_attempts_per_minute(ip_address, now=None):
    """See this module's docstring: a proxy for "failed logins per address per minute" using
    what's actually observable at /detect time (an attempt happened), not the eventual outcome.
    Call this once per real /detect invocation - it records the attempt as a side effect, it
    isn't a read-only query."""
    return _login_attempts.record(ip_address or "", now=now)


def _ip_subnet(ip_address):
    """The /24 (IPv4) or /64 (IPv6) network an address belongs to - coarser than the exact IP,
    so a device roaming within the same ISP/campus block isn't flagged "new" on every request
    merely because DHCP handed it a different address."""
    try:
        ip = ipaddress.ip_address(ip_address)
    except ValueError:
        return ip_address or ""
    if ip.version == 4:
        network = ipaddress.ip_network(f"{ip}/24", strict=False)
    else:
        network = ipaddress.ip_network(f"{ip}/64", strict=False)
    return str(network)


def device_fingerprint(email, user_agent, ip_address):
    """A stable id for "this account, from roughly this network, on this browser/client" -
    not identity-grade, just enough to notice "an account's next login comes from a fingerprint
    never seen before", the signal the proposal asks for."""
    raw = f"{(email or '').strip().lower()}|{user_agent or ''}|{_ip_subnet(ip_address)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _InMemoryDeviceStore:
    """Default device_store for is_new_device(): a per-process set of fingerprints already
    seen. See this module's docstring for why this is a starting point, not durable tracking."""

    def __init__(self):
        self._seen = set()
        self._lock = threading.Lock()

    def seen_before(self, fingerprint):
        with self._lock:
            if fingerprint in self._seen:
                return True
            self._seen.add(fingerprint)
            return False


_device_store = _InMemoryDeviceStore()


def is_new_device(email, user_agent, ip_address, device_store=None):
    """1 if this exact (account, device-ish, network) fingerprint has never been recorded
    before, else 0. Records the fingerprint as a side effect (same "record while reading" shape
    as login_attempts_per_minute) so the NEXT login from this device reads 0, not just the
    first. Pass device_store explicitly in tests instead of relying on the shared module-level
    store, so tests don't leak state into each other."""
    store = device_store if device_store is not None else _device_store
    fp = device_fingerprint(email, user_agent, ip_address)
    return int(not store.seen_before(fp))


def extract_features(email, password, ip_address, user_agent=None, now=None, device_store=None):
    """Every feature in one place, in a stable key order - what /detect's eventual model-input
    assembly and the SHAP explanation should both read from, so they can never compute the set
    two different ways and drift apart."""
    return {
        "payload_length": payload_length(email, password),
        "unusual_character_ratio": unusual_character_ratio(f"{email or ''}{password or ''}"),
        "has_sql_pattern": has_sql_pattern(email, password),
        "has_xss_pattern": has_xss_pattern(email, password),
        "login_attempts_per_minute": login_attempts_per_minute(ip_address, now=now),
        "is_new_device": is_new_device(email, user_agent, ip_address, device_store=device_store),
    }
