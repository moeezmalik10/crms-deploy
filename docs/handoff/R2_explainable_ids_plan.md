# R2 handoff: explainable, request-level intrusion detection

**For:** Laiba Azhar
**Written by:** an AI assistant working with Muhammad Moeez on his own R3 scope, at his request,
as a starting point for you — not a substitute for your own work. Everything here is a plan to
review, adjust, and implement yourself; nothing in this file has been coded for you. This is also
the area with the most ML work (new training data, a new model, SHAP) of any of the remaining
pieces, so treat the "suggested order" section as a starting point, not a deadline.

---

## What the proposal asks for (Component 9, Objective 6)

> "Detection moves from the browser to the server, where it cannot be bypassed. The model is
> trained on features taken from web requests, such as failed logins per address per minute,
> request rate, payload length, unusual characters, known injection patterns and new devices.
> Public HTTP attack data (CSIC 2010) is combined with attacks we simulate against our own test
> deployment. Every alert shows the three features that most influenced the decision, using SHAP,
> and suspicious addresses are blocked only after an administrator reviews them."

Success criteria (Objective 6 / M3): F1-score ≥ 0.90 on a held-out test set; top three reasons
shown with every alert.

## What's already done (so you don't duplicate it)

- Server-side screening can't be bypassed by calling the backend directly — `auth_routes.py`'s
  login route calls the IDS service itself (`_ids_block_reason`), fixed earlier this session. The
  IDS can't be skipped by going around the website.
- Rule-based SQL-injection and XSS detection already run server-side on every `/detect` call
  (`ids/ids/sql_detector.py`, `ids/ids/xss_detector.py`), ahead of the ML model.
- `model_loader.py`'s RCE vector (unpickling an unauthenticated public download) was fixed this
  session — pinned SHA-256 hash + a restricted unpickler that only allows the exact classes the
  current model needs. **This matters directly for your work**: when you train and ship a new
  model, you'll need to update `EXPECTED_SHA256`/`FILE_ID` together, and — if your new model uses
  different classes than `RandomForestClassifier`/`DecisionTreeClassifier`/`Tree` (e.g. if you
  pick XGBoost, logistic regression, or anything else) — extend `ALLOWED_CLASSES` to match. Don't
  skip this; it's what stands between a future compromised model file and real code execution on
  the server.
  - To find the right allowlist for whatever model you train: pickle it, then walk the pickle's
    own opcode stream with a `find_class()` override that *records* what's requested instead of
    constructing it — this is exactly what was done to build the current allowlist, and it's
    safer than guessing or trusting the file blindly. See the committed version of
    `ids/ids/model_loader.py` (git history around the "Fix the IDS model_loader.py RCE vector"
    commit) for the discovery pattern used, and adapt it for your own model file.
- The current model (a `RandomForestClassifier` on CICIDS2017-style **network-flow** features —
  packet counts, flow duration, etc.) is what you're replacing. It's real, it loads, it's not a
  stub — but per the proposal it's the *wrong kind of model* for this system now: CRMS's `/detect`
  endpoint only ever receives request-level data (email, password, mode, task_type — see
  `ids/routes/detect.py`), never network flow data, so the current model's own `features` list
  (`Destination Port`, `Flow Duration`, etc.) is structurally mismatched with what it's actually
  being asked to predict on in production today.

## What's genuinely missing

### 1. Request-level feature extraction — built, not yet wired into `/detect`
`ids/ids/features.py` (new) has all six features as pure, tested functions
(`backend/tests` sibling `ids/tests/test_features.py`, 23 tests), per the proposal's list:
- **"Failed logins per address per minute"** is approximated as *login attempts* per address per
  minute (`login_attempts_per_minute`, a sliding-window counter). Documented explicitly in the
  module docstring why: `auth_routes.py::_ids_block_reason` calls `/detect` *before* the password
  check, so the real pass/fail outcome isn't known yet at screening time - attempt rate is what's
  actually observable there, and is the standard proxy for brute-force/credential-stuffing
  detection in that situation. Feeding back the real outcome would need the backend to report it
  after the check — a genuine architecture change (new backend→IDS call), not a feature-extraction
  detail; not done here, flagged for a deliberate decision.
- Request rate: same sliding-window mechanism, keyed by IP — `attack_counter.py`'s own counter
  (all-time, Supabase-backed, used only for the existing 3-strikes blacklist trigger) was *not*
  reused, since a rate feature and an all-time tally are different things; `features.py`'s
  `_SlidingWindowCounter` is in-memory per-process instead (see below for the tradeoff).
- Payload length (`payload_length`), unusual-character ratio (`unusual_character_ratio`).
- Known injection patterns: `sql_detector.py`/`xss_detector.py`'s booleans ARE exposed as features
  now (`has_sql_pattern`/`has_xss_pattern`), but `routes/detect.py`'s early-return behavior for a
  rule-based hit is unchanged — so in the live `/detect` flow these two features will typically
  read 0 whenever the ML model actually runs, since a true hit already returned before reaching
  it. Useful for training-time context and the SHAP explanation regardless; just don't expect to
  see them flip to 1 in a live prediction.
- "New device": `device_fingerprint()` hashes `email + user-agent + /24-or-/64 IP subnet`
  (subnet, not exact IP, so DHCP reshuffling inside the same lab doesn't look like a new device).
  `is_new_device()` checks it against a store — the default (`_InMemoryDeviceStore`) is an
  in-memory per-process set: correct for one Flask process, but **resets on restart and isn't
  shared across gunicorn workers if this service is ever scaled**. Durable tracking needs a new
  Supabase table (same pattern as `intrusion_logs`/`blacklisted_ips` in
  `database/supabase_setup.sql`) — deliberately not added without a decision on it; the function
  takes a `device_store` param so swapping in a real backing store later doesn't touch the
  fingerprint logic.

**Wired in**: `routes/detect.py` now calls `extract_features()` on every request (before the
blacklist/SQL/XSS short-circuits, so the stateful counters see every attempt including blocked
ones) and returns it as a `"features"` key alongside `"prediction"` in every response branch.
4 new route-level tests in `ids/tests/test_detect_route.py`. Not yet fed into a prediction or
persisted anywhere - returned in the response only, since logging real vectors needs the same
new-table decision as the device-fingerprint store above, not made here.

**A real double-counting bug this wiring exposed**: `/detect` is called from *two* places for
every login - `frontend/Components/LoginPage/LoginPage.jsx` calls it directly from the browser
first (a leftover from before server-side screening existed), and only if that passes does the
frontend call `/auth/login`, which makes `auth_routes.py::_ids_block_reason` call `/detect`
*again*, server-side. Both calls now increment `login_attempts_per_minute` and consume
`is_new_device`'s one-time flag, so every real login attempt counts as two and a genuinely new
device reads "new" on the first call but not the second. The frontend's own direct call is
already redundant for security (the backend's server-side call can't be bypassed, which is the
whole reason it was added - see `SESSION_SUMMARY.md` §1) - removing `LoginPage.jsx`'s direct
`/detect` call would fix the double-counting and drop dead code in one change, but that's a
frontend behavior change outside pure feature-extraction scope, so it wasn't done without
flagging it first.

### 2. Training data and training pipeline
- CSIC 2010 HTTP dataset (public, a proper download+citation, not an unauthenticated pickle this
  time — keep the loading pattern honest from day one).
- Self-simulated attacks against your own test deployment, as the proposal specifies — plan how
  you'll generate and label this safely (a disposable local/staging instance, not production).
- A real train/test split and the F1 ≥ 0.90 measurement the proposal commits to — decide now how
  you'll report this number, since "F1-score of 0.90 or higher on a held-out test set" is a
  specific, checkable claim your final report will need to back up with an actual held-out set,
  not just training accuracy.

### 3. SHAP integration
Not present anywhere in the codebase (`ids/requirements.txt` has no `shap` dependency yet). Once
you have a trained model:
- Add `shap` to `ids/requirements.txt`.
- Compute SHAP values per prediction in `ids/ids/detector.py::predict_attack` (or a new module
  alongside it) and return the top 3 feature contributions, not just the predicted class.
- Extend `ids/routes/detect.py`'s response shape to include those three reasons.
- Update the frontend: `frontend/Components/IDS/AdminSecurityDashboard.jsx` currently (check its
  current state — it may only show raw predictions/blacklist entries today) would need a section
  rendering "why" per alert, matching the proposal's "every alert shows the three features that
  most influenced the decision."

### 4. Keep the existing safety behavior
`model_loader.py`'s fallback ("model unavailable → rule-based detection only, service still
starts") must keep working with your new model too — don't remove that try/except, and make sure
your new model's absence/corruption still degrades gracefully rather than taking the whole IDS
service down. Confirmed working as of this session; `ids/tests/test_model_loader.py` has the test
coverage pattern to extend once you swap in a new file.

## Suggested order (status as of 2026-10-21)
1. ~~Nail down the exact feature list~~ — done (item 1 above): six features, each with its
   design tradeoff documented in `ids/ids/features.py`'s module docstring.
2. ~~Build the counters/extractors as testable functions~~ — done, 23 tests in
   `ids/tests/test_features.py`.
3. ~~Wire `extract_features()` into `routes/detect.py`~~ — done, 4 route tests in
   `test_detect_route.py`. **Fix the double-counting bug this exposed first** (item 1's note on
   `LoginPage.jsx`'s redundant direct `/detect` call) before trusting `login_attempts_per_minute`/
   `is_new_device` numbers from real traffic.
4. **Next, not started**: decide whether/where to log computed feature vectors for future
   training data (same new-Supabase-table decision as the device-fingerprint store).
5. Assemble training data (CSIC 2010 - a real download, needs sign-off before fetching it; plus
   self-simulated attacks), train, measure F1 on a real held-out set.
6. Ship the model the safe way (pin hash, check/extend the unpickler allowlist - the pattern in
   `model_loader.py` already does this for the current model).
7. Add SHAP, wire it into `/detect`'s response, then the dashboard.

## Testing
`ids/tests/` now has **35 tests**: 8 for `model_loader.py`'s security gates, 23 for
`features.py` (`test_features.py`), 4 for the wired-in `/detect` route
(`test_detect_route.py`) — deterministic (no real time.sleep, no real network: the route tests
rely on no `SUPABASE_URL` being set in this environment, which makes `is_blacklisted`/
`count_attacks` fail fast on a malformed relative URL rather than actually reaching Supabase,
and monkeypatch `log_intrusion` itself where a response branch would otherwise call it - that
function calls a real third-party geolocation API before ever touching Supabase). Covers the
sliding window's expiry behavior, subnet-based fingerprint stability, and each feature
function's edge cases (empty/missing input). Full suite passes, no regressions.
