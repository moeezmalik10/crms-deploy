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

### 1. Request-level feature extraction
None of this exists yet. You'll need, at minimum, per the proposal's own list:
- Failed logins per address per minute — needs a sliding-window counter per IP. `ids/ids/
  attack_counter.py` already does *something* adjacent (it's used for the "3 strikes" blacklist
  trigger in `ids/routes/detect.py`) — check whether it can be extended/reused rather than
  building a second counting mechanism from scratch.
- Request rate (per IP, per account).
- Payload length, unusual-character ratio.
- Known injection patterns — `sql_detector.py`/`xss_detector.py` already have pattern-matching
  logic; whether their boolean outputs become FEATURES for the ML model (not just independent
  pre-filters) is a design choice worth making deliberately, not by accident.
- "New device" — needs some notion of a known-device fingerprint per account (nothing like this
  exists today; decide what's feasible — e.g. a hash of email+user-agent+IP-subnet seen before).

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

## Suggested order
1. Nail down the exact feature list and how each one gets computed from a live request (this is
   the part everything else depends on — do it on paper/in a notebook before touching production
   code).
2. Build the counters/extractors as their own testable functions, independent of the model.
3. Assemble training data, train, measure F1 on a real held-out set.
4. Ship the model the safe way (pin hash, check/extend the unpickler allowlist).
5. Add SHAP, wire it into `/detect`'s response, then the dashboard.

## Testing
`ids/tests/` exists now (created this session, 8 tests for `model_loader.py`'s security gates —
see that file for the project's pytest conventions for this service). Your feature-extraction
functions should be unit-testable the same way, independent of the trained model itself (feed
known inputs, assert the counters/flags come out right) — that's valuable test coverage you can
write *before* the model even exists.
