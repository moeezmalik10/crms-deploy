"""Self-simulated attack traffic (the proposal's other data source, alongside CSIC 2010:
"Public HTTP attack data (CSIC 2010) is combined with attacks we simulate against our own test
deployment").

Safety: this never touches a real network, a real server process, or production. Every event is
computed by calling ids.features.extract_features() directly, in-process - the exact function
routes/detect.py calls on a real request, just driven by a generator instead of an HTTP client.
No Flask app is even started. "Against our own test deployment" is satisfied by exercising the
real detection code path, not by sending traffic anywhere.

Two things CSIC 2010 could not give the trained model, which this can:
1. login_attempts_per_minute and is_new_device - CSIC has no timestamps, IPs, or account
   identity (see train_model.py's docstring). Here, every event carries a real simulated
   timestamp, source IP, and account email, processed in chronological order through the same
   stateful counters production uses, so these two features get genuine values instead of being
   excluded.
2. Login-shaped "normal" text. CSIC's Normal class is e-commerce URLs
   (http://localhost:8080/tienda1/...?id=3&nombre=...) - structurally nothing like an
   email/password pair. A model that only ever saw URL-shaped "normal" traffic may not
   generalize cleanly to what /detect actually receives. Every benign event here is a real
   login-shaped (email, password) pair instead.

A deliberate design constraint, to avoid teaching the model something false: is_new_device=1 is
NOT by itself used as an attack signal anywhere below. A brand new real student's first-ever
login is new-device and completely legitimate - plenty of benign events here are exactly that.
Attack events that happen to involve a new device always pair it with a genuine second signal
(a burst of attempts, or injection-style text), never new-device alone.

A second, more serious problem found only by testing a trained model against real-shaped login
traffic (see docs/handoff/R2_explainable_ids_plan.md item 2b): a CSIC-2010-only model flagged
*every* realistic benign login as anomalous, because CSIC's Normal class is e-commerce URLs
(100+ characters, URL-encoded, query-string-heavy) - structurally nothing like a short
email/password pair, so the model learned "URL-shaped = normal" and anything short read as an
outlier regardless of content. The fix applied: CSIC's rows are no longer used as training
examples at all (neither class) - instead, CSIC_PAYLOAD_POOL (below) extracts the genuinely
attack-indicative *substrings* from its 25,065 real Anomalous rows (SQLi, XSS, CRLF/header
injection, SSI includes, time-based blind SQLi...) and reuses them as realistic attack text
*inside* login-shaped fields, which keeps CSIC's real attack-pattern diversity while fixing the
shape mismatch that caused the false positives.

A third problem, found the same way (testing live behavior, not trusting the held-out metric -
see item 2c): even with the fix above, a hand-rolled benign-password generator (a handful of
words and length/shape rules) has its own implicit statistical signature a model can partly
learn to recognize *as* "normal", instead of generalizing to genuinely varied real passwords -
measured at a 24.3% false-positive rate on an independently-generated benign-login test. The fix
applied here: PASSWORD_CORPUS (below) draws benign passwords from a real public
password-frequency list (100,000 real passwords from a well-known breach-derived corpus, see
training_data/common_passwords/README.md) instead of a small hand-written generator, weighted
toward the more common entries to match the real-world skew where a small number of passwords
account for most actual usage - the same "use real data instead of guessing its shape" fix
already applied to the attack side via CSIC_PAYLOAD_POOL.
"""
import hashlib
import itertools
import os
import random
import re
import string
import sys
import urllib.parse

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # import ids.*

from ids.features import REQUEST_RATE_WINDOW_SECONDS, _InMemoryDeviceStore, _SlidingWindowCounter, extract_features

RANDOM_SEED = 42
SIM_START = 1_700_000_000.0   # an arbitrary fixed epoch - only relative spacing matters
_TRAINING_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "training_data")
CSIC_CSV_PATH = os.path.join(_TRAINING_DATA_DIR, "csic_2010", "csic_database.csv")
PASSWORD_CORPUS_PATH = os.path.join(_TRAINING_DATA_DIR, "common_passwords",
                                    "xato-net-10-million-passwords-100000.txt")

FIRST_NAMES = ["ali", "sara", "hamza", "ayesha", "bilal", "mariam", "usman", "zainab", "faisal",
              "hina", "noman", "areeba", "talha", "laiba", "moeez", "amna", "danish", "sana"]
LAST_NAMES = ["khan", "ahmed", "malik", "butt", "chaudhry", "raza", "hussain", "iqbal",
             "sheikh", "qureshi", "abbasi", "farooq", "siddiqui", "akhtar", "mahmood"]
DOMAIN = "uog.edu.pk"
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/119.0",
]
# A small, hand-picked, always-available set (kept even though CSIC_PAYLOAD_POOL below is much
# larger) so the simulation still works if the CSIC CSV isn't present on disk.
SQLI_PAYLOADS = ["' OR '1'='1", "' OR 1=1 --", "admin'--", "' UNION SELECT username,password FROM user--",
                 "1' AND SLEEP(5)--"]
XSS_PAYLOADS = ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "javascript:alert(1)",
               "<svg onload=alert(1)>"]


def _load_csic_payload_pool():
    """Real attack-indicative substrings extracted from CSIC 2010's 25,065 Anomalous rows - not
    the rows themselves (see module docstring for why), just the genuinely suspicious fragment
    each one carries, reused as login-field-shaped attack text. Returns [] if the CSV isn't
    present (training_data/csic_2010/README.md explains how to get it) rather than raising, so
    callers can fall back to the small hand-picked lists above.

    Extraction: the value of the last `key=value` pair in the row's content (POST body) or URL
    (query string), double-URL-decoded (CSIC's own payloads are frequently double-encoded, e.g.
    %253C is an encoded %3C which is an encoded <). Fragments still containing "://" or
    "localhost" are dropped - those are the static-resource-probing/file-disclosure attacks
    whose entire URL path *is* the payload, which doesn't translate into a login field the way a
    SQLi/XSS/CRLF value does (nobody's email or password field would contain a file path an
    attacker is probing for)."""
    if not os.path.exists(CSIC_CSV_PATH):
        return []
    df = pd.read_csv(CSIC_CSV_PATH, encoding="latin-1")
    anomalous = df[df["classification"] == 1]

    pool = set()
    for row in anomalous.itertuples():
        text = str(row.content) if pd.notna(row.content) else str(row.URL)
        text = re.sub(r"\s*HTTP/1\.\d\s*$", "", text)
        if "=" not in text:
            continue
        frag = text.rsplit("=", 1)[-1].strip()
        try:
            frag = urllib.parse.unquote(urllib.parse.unquote(frag))
        except Exception:
            continue
        if not (3 <= len(frag) <= 200):
            continue
        if "://" in frag or "localhost" in frag.lower():
            continue
        pool.add(frag)
    return sorted(pool)


CSIC_PAYLOAD_POOL = _load_csic_payload_pool()


def _load_password_corpus():
    """100,000 real passwords from a public breach-derived frequency list (see
    training_data/common_passwords/README.md for provenance), sorted most-to-least common in the
    source file - that order IS rank, used below to weight sampling toward the common end,
    matching the real-world Zipfian skew where a small number of passwords account for a large
    share of actual usage. Returns ([], []) if the file isn't present, same fallback contract as
    _load_csic_payload_pool() above."""
    if not os.path.exists(PASSWORD_CORPUS_PATH):
        return [], []
    with open(PASSWORD_CORPUS_PATH, encoding="utf-8", errors="ignore") as f:
        corpus = [line.strip() for line in f if line.strip()]
    weights = (1.0 / (i + 1) for i in range(len(corpus)))
    cum_weights = list(itertools.accumulate(weights))
    return corpus, cum_weights


PASSWORD_CORPUS, _PASSWORD_CUM_WEIGHTS = _load_password_corpus()


def _random_password(rng):
    """Benign password text. Mostly drawn from PASSWORD_CORPUS (real passwords, weighted toward
    the common end) - the fix for the 24.3% false-positive rate measured against the previous,
    hand-rolled-only generator (see module docstring). The remaining share, and the fallback if
    the corpus file isn't present, is a small synthetic generator covering password shapes an
    old breach corpus underrepresents (modern symbol+digit complexity-policy style passwords)."""
    if PASSWORD_CORPUS and rng.random() < 0.75:
        return rng.choices(PASSWORD_CORPUS, cum_weights=_PASSWORD_CUM_WEIGHTS, k=1)[0]

    words = ["summer", "campus", "gujrat", "dragon", "falcon", "cricket", "cloud", "river",
             "mountain", "silver", "golden", "winter", "coffee", "music", "pixel", "rocket"]
    shape = rng.randint(0, 3)
    if shape == 0:
        return f"{rng.choice(words).capitalize()}{rng.randint(1, 9999)}{rng.choice('!@#$%')}"
    if shape == 1:
        return "".join(rng.choices(string.ascii_lowercase + string.digits, k=rng.randint(6, 12)))
    if shape == 2:
        return f"{rng.choice(words)}{rng.choice(words).capitalize()}{rng.randint(1, 99)}"
    return "".join(rng.choices(string.ascii_letters + string.digits + "!@#$%^&*", k=rng.randint(8, 16)))


def _student_email(i):
    """A stable (account index -> email) mapping, deterministic and pure in `i` - every event
    generator below references students by the same small index pool (0-199 or 0-399), and
    is_new_device/login_attempts_per_minute depend on a stable per-account identity, not a fresh
    email each call, so this must return the exact same string for the same `i` every time.

    Previously just f"{first_name}{i // 18}@{DOMAIN}" - 18 names x a 1-2-digit index, one fixed
    domain, ~400 shapes total. Tested live against a trained model (see
    docs/handoff/R2_explainable_ids_plan.md item 2e) and found to cause a 40-60% false-positive
    rate on realistically-varied real accounts, because real accounts here are admin-created
    (backend/app/admin_routes.py's create_user()) with no single fixed local-part convention -
    dotted names, underscored names, and roll-number-style numerics (matching the FYP proposal
    document's own "23011556-099" numbering) are all equally plausible, and the model had only
    ever seen the one narrow shape as "normal". This picks from five such shapes per index
    instead of one, the same "stop guessing the shape, cover the real range" fix already applied
    to passwords via PASSWORD_CORPUS and to attack text via CSIC_PAYLOAD_POOL."""
    r = random.Random(i)
    first = FIRST_NAMES[i % len(FIRST_NAMES)]
    last = LAST_NAMES[(i * 7 + 3) % len(LAST_NAMES)]
    shape = r.randint(0, 4)
    if shape == 0:
        local = f"{first}.{last}"
    elif shape == 1:
        local = f"{first}_{last}{r.randint(1, 99)}"
    elif shape == 2:
        local = f"{first}{r.randint(1, 999)}"
    elif shape == 3:
        # Roll-number style: admission year + department/program code + serial.
        local = f"{r.randint(19, 24)}0{r.randint(10000, 19999)}{r.randint(100, 999)}"
    else:
        local = f"{first}.{last}{r.randint(1, 99)}"
    return f"{local}@{DOMAIN}"


def _lab_ip(subnet, host):
    return f"10.20.{subnet}.{host}"


def _attacker_ip(i):
    """A distinct-looking external IP per index. Originally a linear formula
    (i*7 % 256, i*13 % 256, i*29 % 256) - that has period 256 (every coefficient is coprime to
    256, so i and i+256 collide exactly), which meant hundreds of "different" simulated
    attackers actually shared the same few IPs and inflated login_attempts_per_minute for
    reasons that had nothing to do with the scenario being simulated. A hash spreads i across
    the full address space instead, so collisions are rare and incidental rather than
    guaranteed every 256 calls."""
    h = hashlib.md5(f"attacker-{i}".encode()).hexdigest()
    return f"185.{int(h[0:2], 16)}.{int(h[2:4], 16)}.{int(h[4:6], 16)}"


def _benign_normal_events(rng, n_students, events_per_student, t):
    """Ordinary students logging in a handful of times over a simulated span, mostly from the
    same lab subnet, occasionally a different one (a different PC in the same lab - still the
    same /24, so is_new_device won't fire purely from that, see device_fingerprint's subnet
    rounding) and sometimes genuinely a new device (a different lab entirely). Spaced minutes to
    hours apart - realistic usage, not a burst."""
    events = []
    for s in range(n_students):
        email = _student_email(s)
        home_subnet = 20 + (s % 5)
        ua = rng.choice(UAS)
        for _ in range(events_per_student):
            t += rng.uniform(300, 14400)   # 5 min to 4 hours between this student's own logins
            subnet = home_subnet if rng.random() < 0.85 else rng.randint(20, 40)
            ip = _lab_ip(subnet, rng.randint(2, 254))
            events.append({
                "email": email, "password": _random_password(rng), "ip": ip,
                "ua": ua, "t": t, "label": 0, "kind": "benign_normal",
            })
    return events, t


def _benign_typo_events(rng, n, t):
    """A student mistyping their own password - still completely benign text, just not an exact
    match to any "real" password list; makes sure the model doesn't learn "slightly unusual
    password string" as inherently suspicious."""
    events = []
    for i in range(n):
        t += rng.uniform(60, 3600)
        email = _student_email(rng.randint(0, 200))
        pw = _random_password(rng)
        typo = pw[:-1] if rng.random() < 0.5 else pw + rng.choice("xzq")
        events.append({
            "email": email, "password": typo, "ip": _lab_ip(rng.randint(20, 40), rng.randint(2, 254)),
            "ua": rng.choice(UAS), "t": t, "label": 0, "kind": "benign_typo",
        })
    return events, t


def _benign_quick_retry_events(rng, n, t):
    """A student mistypes their password and retries once or twice within a few seconds -
    genuinely benign, but NOT rate=1 like every other benign event here. Without this,
    login_attempts_per_minute perfectly separates the two classes (every Normal event has rate
    exactly 1), which is an unrealistically easy version of the problem - real brute-force
    detection has to tolerate some benign bursts too."""
    events = []
    for i in range(n):
        t += rng.uniform(300, 7200)
        email = _student_email(rng.randint(0, 200))
        ip = _lab_ip(rng.randint(20, 40), rng.randint(2, 254))
        ua = rng.choice(UAS)
        burst_t = t
        for _ in range(rng.randint(2, 3)):
            burst_t += rng.uniform(2, 8)
            events.append({
                "email": email, "password": _random_password(rng), "ip": ip, "ua": ua,
                "t": burst_t, "label": 0, "kind": "benign_quick_retry",
            })
        t = burst_t
    return events, t


def _attack_injection_events(rng, n, t):
    """Attack payloads in login-shaped (email, password) fields instead of CSIC's URL/query
    fields - same attack text, the field shape production actually sees. Mostly drawn from
    CSIC_PAYLOAD_POOL (real, diverse attack substrings extracted from CSIC's own 25,065
    Anomalous rows - SQLi, XSS, CRLF injection, SSI includes, time-based blind SQLi and more),
    topped up with the small hand-picked SQLI_PAYLOADS/XSS_PAYLOADS list for guaranteed basic
    coverage even if the CSIC CSV isn't present. Timing is ordinary (not bursty) - the payload
    text itself is the signal here, not rate."""
    pool = CSIC_PAYLOAD_POOL if CSIC_PAYLOAD_POOL else (SQLI_PAYLOADS + XSS_PAYLOADS)
    events = []
    for i in range(n):
        t += rng.uniform(60, 7200)
        payload = rng.choice(pool)
        target_field = rng.random() < 0.5
        email = payload if target_field else f"attacker{i}@{DOMAIN}"
        password = payload if not target_field else _random_password(rng)
        events.append({
            "email": email, "password": password, "ip": _attacker_ip(i),
            "ua": rng.choice(UAS), "t": t, "label": 1, "kind": "attack_injection",
        })
    return events, t


def _attack_bruteforce_events(rng, n_attackers, attempts_per_attacker, t):
    """One source hammering many credential guesses in a short window - the
    login_attempts_per_minute signal CSIC could never provide. Individual attempts can look
    textually ordinary; the rate is what's suspicious."""
    events = []
    for a in range(n_attackers):
        ip = _attacker_ip(1000 + a)
        ua = rng.choice(UAS)
        target_email = _student_email(rng.randint(0, 200))   # credential stuffing against a real-looking account
        burst_t = t
        for _ in range(attempts_per_attacker):
            burst_t += rng.uniform(1, 4)   # several attempts within a handful of seconds
            events.append({
                "email": target_email, "password": _random_password(rng) + str(rng.randint(0, 999)),
                "ip": ip, "ua": ua, "t": burst_t, "label": 1, "kind": "attack_bruteforce",
            })
        t = burst_t + rng.uniform(300, 1800)
    return events, t


def _attack_takeover_combo_events(rng, n, t):
    """A new device AND a burst, together - the conjunction that's actually suspicious (see
    module docstring: new-device alone is never the sole signal for an attack label)."""
    events = []
    for i in range(n):
        ip = _attacker_ip(2000 + i)   # a source never used anywhere else in this simulation
        ua = rng.choice(UAS)
        email = _student_email(rng.randint(0, 200))
        burst_t = t
        for _ in range(rng.randint(3, 6)):
            burst_t += rng.uniform(1, 5)
            events.append({
                "email": email, "password": _random_password(rng), "ip": ip, "ua": ua,
                "t": burst_t, "label": 1, "kind": "attack_takeover_combo",
            })
        t = burst_t + rng.uniform(300, 1800)
    return events, t


def generate_events():
    rng = random.Random(RANDOM_SEED)
    t = SIM_START
    all_events = []

    # Scaled up once CSIC_PAYLOAD_POOL (256 real attack substrings) replaced the original 9
    # hand-picked strings as attack_injection's main source - more events means less repeat-rate
    # per payload and a correspondingly richer benign side so the classes stay balanced.
    e, t = _benign_normal_events(rng, n_students=400, events_per_student=10, t=t); all_events += e
    e, t = _benign_typo_events(rng, n=700, t=t); all_events += e
    e, t = _benign_quick_retry_events(rng, n=400, t=t); all_events += e
    e, t = _attack_injection_events(rng, n=2500, t=t); all_events += e
    e, t = _attack_bruteforce_events(rng, n_attackers=180, attempts_per_attacker=6, t=t); all_events += e
    e, t = _attack_takeover_combo_events(rng, n=220, t=t); all_events += e

    # Chronological order matters: is_new_device/login_attempts_per_minute are stateful and
    # order-sensitive, same as real traffic arriving over time.
    all_events.sort(key=lambda ev: ev["t"])
    return all_events


def build_dataset():
    """Runs every event through the real extract_features() in time order, with one fresh
    device_store and one fresh rate counter for the whole simulation - both explicitly local to
    this call, not ids.features' shared module-level instances. Without this, calling
    build_dataset() more than once in the same process (exactly what repeated test runs do)
    would silently accumulate hits across calls, since the shared counters never reset -
    build_dataset() needs to be safely callable more than once and get the same answer every
    time, including in training/train_combined_model.py importing this alongside other code
    that may also exercise ids.features."""
    events = generate_events()
    store = _InMemoryDeviceStore()
    rate_counter = _SlidingWindowCounter(REQUEST_RATE_WINDOW_SECONDS)
    rows = []
    labels = []
    kinds = []
    for ev in events:
        feats = extract_features(ev["email"], ev["password"], ev["ip"], user_agent=ev["ua"],
                                 now=ev["t"], device_store=store, rate_counter=rate_counter)
        rows.append(feats)
        labels.append(ev["label"])
        kinds.append(ev["kind"])
    X = pd.DataFrame(rows)
    y = pd.Series(labels, name="classification")
    kind = pd.Series(kinds, name="kind")
    return X, y, kind


if __name__ == "__main__":
    X, y, kind = build_dataset()
    print(f"{len(X)} simulated events")
    print(y.value_counts().rename({0: "Normal (0)", 1: "Anomalous (1)"}))
    print("\nBy kind:")
    print(kind.value_counts())
    print("\nis_new_device by label (sanity check - should NOT be 100% new-device=attack):")
    print(pd.crosstab(y, X["is_new_device"]))
    print("\nlogin_attempts_per_minute summary by label:")
    print(X.groupby(y)["login_attempts_per_minute"].describe())
