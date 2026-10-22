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

### 1. Request-level feature extraction — built and wired into `/detect`
`ids/ids/features.py` (new) has twelve features as pure, tested functions (`ids/tests/
test_features.py`, 31 tests) - the original six per the proposal's list, plus six more granular
ones added mid-training once the original six plateaued below the F1 target (see item 2):
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

**A real double-counting bug this wiring exposed, now fixed**: `/detect` used to be called from
*two* places for every login - `frontend/Components/LoginPage/LoginPage.jsx` called it directly
from the browser first (a leftover from before server-side screening existed), and only if that
passed did the frontend call `/auth/login`, which makes `auth_routes.py::_ids_block_reason` call
`/detect` *again*, server-side. Both calls were incrementing `login_attempts_per_minute` and
consuming `is_new_device`'s one-time flag, so every real login attempt counted as two. Fixed by
removing `LoginPage.jsx`'s direct call - the backend's server-side call can't be bypassed, which
is the whole reason it was added (see `SESSION_SUMMARY.md` §1), so the frontend's own call was
purely redundant. Verified: logged in against production via a local dev server, confirmed via
network inspection that `/detect` now fires exactly once per login.

### 2. Training data and training pipeline — a real model trained, honest result below target
- **Data**: `ids/training_data/csic_2010/csic_database.csv` (61,065 rows, 36,000 Normal /
  25,065 Anomalous) - the CSIC 2010 HTTP dataset. Original hosting confirmed down by you
  directly; sourced instead from a verified Kaggle mirror (row/label counts checked against the
  official description before trusting it - see `training_data/csic_2010/README.md` for the
  full provenance trail and citation).
- **Pipeline**: `ids/training/train_model.py` - maps CSIC's `URL`/`content` fields through the
  *exact same* `ids.features` functions `routes/detect.py` calls (applied to URL+content instead
  of email+password - a deliberate cross-domain choice, documented in the script's own
  docstring, since the underlying signal - length, unusual characters, injection syntax - is the
  same kind regardless of what the field is named). Real stratified 80/20 train/test split,
  `random_state=42` for reproducibility, `RandomForestClassifier`.
- **A real feature-coverage gap, documented not hidden**: CSIC 2010 has no timestamps, no IP, no
  account identity - a static, unordered dump of independent request rows. Two of
  `ids.features`'s six features (`login_attempts_per_minute`, `is_new_device`) are inherently
  about behavior *across* requests over time, which this dataset structurally can't represent.
  Training on fabricated values for those two would look like full coverage while teaching the
  model nothing real - so the trained model uses only the ten features genuinely derivable from
  a single static request (see next bullet); the other two stay live-computed-but-not-trained in
  `/detect`, same as today, until a data source with real temporal/session structure exists (the
  proposal's own "self-simulated attacks against your own test deployment" is the natural
  candidate for that, since it would be generated live).
- **Six more features added mid-pipeline, once the first four plateaued**: the original minimal
  set (`payload_length`, `unusual_character_ratio`, `has_sql_pattern`, `has_xss_pattern`) capped
  out around F1 0.82-0.84 regardless of tuning. Added `special_character_count`,
  `max_special_char_run`, `url_encoded_count`, `has_path_traversal`, `sql_keyword_count`,
  `digit_ratio` - more granular, still single-value, still explainable companions (a *count* or a
  *run length* instead of a bare boolean), not a move toward opaque features. All ten are now
  computed in production `/detect` too (`ids/ids/features.py`), not just at training time.
- **Honest final result**: `random_state=42`, `class_weight="balanced_subsample"`,
  500 trees → **F1 (Anomalous class, the proposal's literal target metric) = 0.8847**; macro F1
  (both classes averaged) = 0.9030. **Does not meet the proposal's F1 ≥ 0.90 commitment** on its
  own stated metric. Several configurations were tried (plain balanced RF, `GradientBoosting`,
  wider trees, different `min_samples_leaf`/`max_features`) and all landed within ~0.001 of each
  other - the real finding is that this is a **feature/data ceiling, not a tuning problem**:
  `has_xss_pattern` and `has_path_traversal` contribute essentially zero importance to the
  trained model (CSIC's XSS/path-traversal examples are apparently already well-separated by the
  other features, or too few to matter), and digit ratio / URL-encoding / unusual-character ratio
  do almost all the real work. **This F1 number, and why it falls short, needs to go in the final
  report honestly** - do not round up or re-run until a flattering split appears; the methodology
  (fixed `random_state`, genuine held-out set, multiple configurations reported not cherry-picked)
  is itself part of what a grader checks.
- **Self-simulated attacks — tried, with an honest, nuanced result (not the clean win you might
  hope for).** `ids/training/simulate_attacks.py` generates labeled login-shaped traffic
  entirely in-process (calls `extract_features()` directly, in real chronological order, through
  a shared device store - never a real network call, real server, or production, satisfying "a
  disposable local/staging instance" in the safest possible way: there is no server at all).
  5,316 events: ordinary student logins (including genuine first-time devices, labeled Normal -
  **new-device alone is never used as an attack signal anywhere in the generator**, since a real
  student's first login isn't an attack), password typos, a few benign quick-retry bursts
  (deliberately added so `login_attempts_per_minute` doesn't trivially separate the classes),
  SQLi/XSS payloads in actual email/password fields (not CSIC's URL query strings), brute-force
  bursts, and new-device-plus-burst "account takeover" attempts (new device is only ever paired
  with a second real signal, never alone). `ids/training/train_combined_model.py` runs two
  experiments:
  - **A real bug caught by writing tests for the simulator, worth knowing about**: the first
    version's `_attacker_ip()` used a linear formula (`i*7 % 256` etc.) that has period 256 -
    hundreds of supposedly distinct simulated attackers collided onto the same handful of IPs,
    inflating `login_attempts_per_minute` for reasons that had nothing to do with the scenario
    being simulated. Caught by `ids/tests/test_simulate_attacks.py::
    test_login_attempts_per_minute_is_not_a_perfect_separator` failing - fixed with an MD5-hash
    spread instead of a linear one. A second, related bug: `build_dataset()` was reusing
    `ids.features`'s *shared* module-level rate counter, so calling it twice in one process (the
    test suite does exactly this) silently accumulated stale hits across calls. Fixed by making
    `login_attempts_per_minute()`/`extract_features()` accept an injectable `counter` (same
    pattern already used for `device_store`), so the simulator can use one genuinely fresh per
    call. **Both numbers below are post-fix** - the pre-fix run of Experiment B originally showed
    F1 = 0.9924, which the fix brought down to a far more believable 0.8839. Left as a cautionary
    note rather than quietly corrected: an unrealistically clean result from your own simulator
    is a reason to look harder for a bug, not a reason to celebrate.
  - **Experiment A — CSIC + simulated, same 10 features**: F1 = 0.8740 at the time (narrow,
    9-payload simulator), *worse* than the CSIC-only baseline (0.8847) - re-running
    `train_combined_model.py` today gives 0.8681 against the now-enriched simulator (item 2c),
    still not better. Adding simulated data did not help on this combined held-out set either
    time - a real, reportable finding, not hidden because it's inconvenient. Plausible reason:
    CSIC's own attack variety (buffer overflow, parameter tampering, static-resource probing,
    file disclosure...) is on a structurally different (URL-shaped) distribution than the
    simulated rows even after the item 2c improvements, so mixing them may still shift the
    learned decision boundary without adding proportionally representative signal.
  - **Experiment B — simulated data only, all 12 features**: F1 = 0.8839 at the time (post
    IP-collision bug fix, narrow simulator). Still
    not a real-world-comparable number - it's trained AND evaluated entirely on the same narrow
    synthetic generator (9 fixed payload strings, rule-based burst/benign timing), so it mostly
    demonstrates the model can learn the generator's own rules, not that it would catch a real,
    novel attack it has never seen the shape of. Its genuine value: feature importances show
    `login_attempts_per_minute` as the #2 most important feature (0.29, behind `payload_length`
    at 0.37) when it's actually available, validating that this feature carries real signal *in
    principle* - useful evidence for the feature design, not a deployable accuracy claim.
  - **Held-out F1 convergence (0.87-0.88 across all three approaches) is real, but it is not the
    whole story - see the next bullet, which found something more important than any of these
    numbers.**

### 2b. A shipping attempt, and why it was reverted - read this before trusting any F1 number above

The CSIC-only model (F1 = 0.8919 after retuning for a deployable file size - see below) was
built out fully: moved into `ids/r2_model.pkl`, `model_loader.py` rewritten to load it
(hash-pinned, allowlist re-derived via the same discovery-unpickler technique for this specific
file), `detector.py` rewritten to call `extract_features()` instead of blindly reindexing the
raw request body, `routes/detect.py` wired to feed it in. All 50+ tests passed. Then, **before
committing any of it**, the actual behavior was tested end to end against realistic login
traffic (not CSIC's held-out split - genuine `(email, password)` pairs through the real Flask
route) - the same "test it live, don't just trust the metric" discipline this whole session has
used everywhere else.

**Every single realistic benign login was flagged "Anomalous Request (ML)".** `student@uog.edu.pk`
/ `Summer2026!`, `admin@uog.edu.pk` / `Admin@12345`, `a@b.com` / `x` - 100% false positive rate,
reproduced across six different test logins. Deployed as-is, this model would have blocked every
real student login (`auth_routes.py::_ids_block_reason` returns 403 "Suspicious activity
detected" for any `prediction != "BENIGN"`) - a self-inflicted, total login outage.

**Root cause, and it was already flagged as a risk before this, just not confirmed as fatal
until now**: `train_model.py`'s own docstring called the URL-vs-login cross-domain choice
"deliberate" and worth watching. CSIC's Normal class is e-commerce URLs
(`http://localhost:8080/tienda1/index.jsp`, routinely 100+ characters, URL-encoded, slash- and
query-heavy); a real login is a short `email`+`password` pair with none of that structure. The
model learned "CSIC's idea of normal," which numerically has almost nothing in common with what
`/detect` actually receives - so it read every short, plain login as an outlier, regardless of
content. **The held-out F1 (0.8919) measured on CSIC's own test split was never wrong - it was
answering a question (does this generalize within CSIC's own distribution?) that is not the
question that matters for deployment (does this generalize to what `/detect` actually sees?).**

**The simulated-data model (Experiment B, F1=0.8839, trained on genuine login-shaped text) was
checked too, as a natural comparison - 2 of 4 benign logins correct, better than CSIC's 0/4 but
still not reliable.** Consistent with the root cause above: a model is only as good on real
traffic as its training data resembles that traffic, and neither CSIC (URL-shaped) nor the
current simulator (a narrow, rule-generated vocabulary) resembles it closely enough yet.

**Reverted, cleanly, before anything unsafe reached a commit**: `model_loader.py`, `detector.py`,
`requirements.txt` (gdown restored) and the two affected test files are back to exactly their
last-committed state - production `/detect` still predicts from the old (admittedly also
non-functional, but *inert rather than actively harmful* - see item introduction above) model,
unchanged. `ids/r2_model.pkl` was removed from the shipped location (kept at
`ids/training/output/r2_model.pkl`, gitignored, for reference). **The twelve feature functions,
their tests, the training/simulation scripts, and this writeup are the real, kept output of this
attempt - a trained, "ship-ready-looking" model was not, because it would have been actively
harmful, and a good F1 on the wrong distribution is worse than no model, not better.**

**What would actually need to happen before anything ships**: train (or at minimum validate)
against data that genuinely resembles `/detect`'s real traffic shape - either a much larger and
more varied self-simulated login dataset (not 9 fixed payload strings), or hand-labeled samples
of real login attempts (benign and, carefully, adversarial) from this system's own traffic. CSIC
2010 remains useful as a *source of attack-pattern variety* (it has far richer injection
categories than the simulator does) but should not be the *only* data a login-screening model
ever sees "Normal" examples from again - whatever ships next should mix CSIC's attack diversity
with genuinely login-shaped benign examples, and **must be sanity-checked against real-shaped
traffic before being wired into anything that can return a 403, every time, not just checked
against its own held-out split.**

### 2c. Second attempt: real improvement, still not shippable, and a deeper limitation found

Took item 2b's own advice: `training/simulate_attacks.py` no longer uses CSIC *rows* for
training at all. Instead, `CSIC_PAYLOAD_POOL` extracts the genuinely attack-indicative
*substrings* from CSIC's 25,065 real Anomalous rows (SQLi, XSS, CRLF/header injection, SSI
includes, time-based blind SQLi - 256 distinct real payloads after filtering out the
static-file-probing ones that don't translate into a login field), reused as attack text inside
login-shaped fields - keeping CSIC's genuine attack-pattern diversity while fixing the shape
mismatch that caused the false positives. Benign passwords switched from a fixed 8-item list to
a randomized generator (several realistic shapes: word+digits+symbol, lowercase+digits,
word-pairs, full random). Simulation scaled up to 10,262 events. Retrained (simulated-only, the
only source with correct shape and real timestamps/IPs) via `training/train_combined_model.py`'s
Experiment B: **F1 = 0.8959**, best held-out number yet, model only ~4MB - re-run this script to
reproduce; a one-off manual run during development showed 0.8962, consistent within noise.

**Learned from the first attempt: did not trust this number either.** Two real-login tests:
- An initial 8-example check reused the same IP/device-tracking state across all 8 "independent"
  logins by mistake - which made them look like a credential-stuffing burst from one source
  (exactly the pattern the model is supposed to catch), not 8 unrelated students. **This was a
  flaw in the test, not the model** - caught by checking the actual feature values the "wrong"
  predictions were based on (`login_attempts_per_minute` was 2+ for a supposedly single login)
  before concluding anything. Retested with each login given its own fresh, independent
  device/rate state: **7/8 correct**, the one miss an extreme 1-character-password edge case.
- A larger, more rigorous check followed: 300 logins from a *second, independently-seeded*
  generator (different word bank, different RNG, never sharing code with the training
  simulator's generator) with properly isolated state per login. **Result: 24.3% false positive
  rate** - far better than the first model's 100%, but still unacceptable (roughly 1 in 4 real
  logins blocked).

**What the 24% reveals**: a subtler version of the same underlying problem. The training
simulator's own benign-password generator has an implicit statistical "shape" (its specific word
choices, length ranges, digit placement) that a sufficiently expressive model can partly learn
to recognize *as* "normal" - generalizing imperfectly to a differently-shaped (but equally
legitimate) password generator, let alone real human-chosen passwords. This is also a genuinely
harder version of the classification problem than CSIC's original one: CSIC's "normal" (long
URLs) and "anomalous" (short to medium injection strings) were coarsely very different in raw
length/structure, an easy split for these ten statistical features; a real password and a short
injection snippet can both be ~10-15 characters with a similar mix of letters/digits/symbols -
genuinely harder to separate on length/ratio/count features alone, no matter how much
better-shaped the training data gets.

**Bottom line for whoever picks this up**: progress is real (100% -> 24.3% false-positive rate)
but not sufficient. Two honest paths forward, neither attempted here: (a) a much larger and more
realistic benign corpus - ideally drawn from real password-frequency statistics (e.g. a public
breach-derived common-password list, used only for its statistical shape, not to actually guess
real students' passwords) rather than a hand-built generator, so the model learns genuine
real-world password diversity instead of one script's idea of it; or (b) a different feature
family better suited to short-string classification - character-level n-grams, or a lookup
against known-common-password lists, rather than coarse aggregate statistics (length, ratios,
counts) that struggle to distinguish two short strings that happen to look similar in aggregate
even when their content is completely different. Whichever is tried, **the 300-login
independent-generator sanity check above is the right kind of test to re-run before trusting any
future number** - not the held-out F1 alone, which both attempts proved can look good while
being unsafe.

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
1. ~~Nail down the exact feature list~~ — done (item 1 above): twelve features now (six
   original + six added mid-training to close the F1 gap, see item 2), each with its design
   tradeoff documented in `ids/ids/features.py`'s module docstring.
2. ~~Build the counters/extractors as testable functions~~ — done, 31 tests in
   `ids/tests/test_features.py`.
3. ~~Wire `extract_features()` into `routes/detect.py`~~ — done, including the double-counting
   fix (item 1).
4. **Still not started**: decide whether/where to log computed feature vectors for future
   training data (same new-Supabase-table decision as the device-fingerprint store).
5. ~~Assemble training data, train, measure F1~~ — done (item 2 above). Three approaches
   converge at F1 0.87-0.88, but **item 2b found something that matters more than the number
   itself**: the CSIC-only model, despite its F1, flags essentially all real login traffic as
   anomalous (0% correct on realistic benign logins) - a distribution mismatch between CSIC's
   URL-shaped traffic and `/detect`'s actual email/password input. **Read item 2b before
   trusting any F1 number in item 2**, including after a future retrain.
6. **Attempted and reverted** - ship the model the safe way: the full pipeline (hash pin,
   allowlist, `detector.py` rewrite) was built and does work *mechanically* (see item 2b), but
   was reverted before committing because the specific CSIC-only model it would have shipped is
   actively unsafe (blocks real logins). The engineering pattern is proven and reusable; what's
   missing is a model that's actually safe to point it at - see item 2b's closing paragraph for
   what that needs.
7. Add SHAP, wire it into `/detect`'s response, then the dashboard - blocked on 6 for the same
   reason: SHAP explanations from a model that misclassifies real traffic would just be
   confidently-explained wrong answers.

## Testing
`ids/tests/` now has **50 tests**: 8 for `model_loader.py`'s security gates, 31 for
`features.py` (`test_features.py`), 4 for the wired-in `/detect` route
(`test_detect_route.py`), 7 for the attack simulator (`test_simulate_attacks.py` - reproducibility,
chronological ordering, and the two anti-"too easy" checks that catch a simulator accidentally
teaching the model something false or trivial) — deterministic (no real time.sleep, no real
network: the route tests rely on no `SUPABASE_URL` being set in this environment, which makes
`is_blacklisted`/`count_attacks` fail fast on a malformed relative URL rather than actually
reaching Supabase, and monkeypatch `log_intrusion` itself where a response branch would otherwise
call it - that function calls a real third-party geolocation API before ever touching Supabase).
Covers the sliding window's expiry behavior, subnet-based fingerprint stability, and each feature
function's edge cases (empty/missing input). Full suite passes, no regressions.

`ids/training/train_model.py` is a separate, manually-run pipeline (not part of the pytest
suite - needs the 29MB CSIC dataset, which isn't in CI, same precedent as
`backend/tests/load/`/`backend/tests/real_exec/`). Re-run it with
`venv\Scripts\python.exe training\train_model.py` from `ids/` after any change to the trainable
feature functions, to get a fresh, real F1 number rather than trusting a stale one.
