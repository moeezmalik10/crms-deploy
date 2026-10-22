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
but not sufficient. Two honest paths forward: (a) a much larger and more realistic benign corpus
- ideally drawn from real password-frequency statistics rather than a hand-built generator - or
(b) a different feature family better suited to short-string classification. Path (a) was tried
next - see item 2d.

### 2d. Third attempt: a real public password corpus - the biggest improvement yet, genuinely close

Took path (a) from item 2c's own recommendation. `PASSWORD_CORPUS` in
`training/simulate_attacks.py` now draws benign passwords from
[`danielmiessler/SecLists`](https://github.com/danielmiessler/SecLists)'s
`xato-net-10-million-passwords-100000.txt` - 100,000 real passwords from a well-known
breach-derived corpus, weighted toward the common end (matching the real-world Zipfian skew)
instead of a hand-rolled generator's narrow, arbitrary shape. Full provenance in
`training_data/common_passwords/README.md`. 75% of benign passwords now come from this real
corpus, 25% still from the old synthetic generator (covering modern complexity-policy-style
passwords the corpus, being older, underrepresents). Retrained (Experiment B, simulated-only, 12
features): F1 = 0.8972 - again, not trusted on its own.

**Two independent, rigorous re-tests, not one**, specifically because item 2c's F1 number also
looked fine before testing live traffic:
- **300 passwords sampled uniformly from the full real corpus** (not the weighted-toward-common
  distribution training used, so this mostly exercises passwords the model saw rarely or never
  during training) - **2.7% false-positive rate** (8/300). The 8 misses cluster near a genuine
  decision boundary (moderate digit ratio, "word+123"-style passwords like `jimbo123`,
  `pasta123`) - reasonable classifier uncertainty at the margin, not a systematic distributional
  failure the way the first two attempts were.
- **150 passwords from item 2c's own synthetic generator** (the exact one that measured 24.3%
  there) - **35.3% false-positive rate, worse than before.** Expected and explained, not
  swept aside: training now weights 75% toward the real corpus, proportionally de-emphasizing
  that one arbitrary generator's specific style, so the model got better at recognizing real
  passwords and worse at recognizing *that particular synthetic generator's* passwords. This
  doesn't weaken the real-corpus result - production receives real human passwords, not that
  generator's output - but it's reported in full rather than only citing the favorable number.
- **Attack detection did not regress**: 8/8 novel attacks (SQLi, XSS, path traversal, command
  injection - none of the exact strings seen in training) still correctly flagged. A simulated
  brute-force burst (6 rapid attempts, same IP) is caught progressively, starting at the 3rd
  attempt - not instantly trigger-happy on a single retry, not slow to react either.

**Model**: `ids/training/output/r2_model_v3.pkl` (not committed - gitignored, same as every
other trained artifact in this doc), 3.7MB, 12 features, `RandomForestClassifier(n_estimators=150,
max_depth=20, min_samples_leaf=4, class_weight="balanced_subsample")`. **Still not shipped** -
2.7% is a large improvement over every prior attempt but is a judgment call, not an
automatic green light, on whether it's low enough to wire into something that can return a 403;
that decision wasn't made unilaterally here. If it ships: re-derive the unpickler allowlist via
the discovery technique for this specific file (don't assume it matches an earlier model's), and
re-run both the uniform-real-corpus and novel-attack checks above as the actual acceptance test,
not the held-out F1.

### 2e. Fourth attempt: shipped, live-tested through the real Flask route, reverted - a second,
deeper instance of the same root problem

Wired item 2d's model into production (`ids/ids/model_loader.py` rewritten to load the committed
`ids/r2_model.pkl` by pinned hash instead of downloading the old CICIDS2017 model; `ids/ids/detector.py`
rewritten to take the already-computed feature dict and map 0/1 to `BENIGN`/`Anomalous Request (ML)`;
`routes/detect.py` updated to call it). All 52 tests passed, including a new route-level regression
test asserting realistic benign logins aren't flagged.

That test used five different fabricated "users," each given their own source IP via
`environ_overrides` (first attempt forgot this and all five came back flagged - an exact repeat of
the shared-rate-counter artifact from item 2c, this time one layer up, at the Flask-route level
instead of inside a one-off validation script; fixed the same way, by varying `REMOTE_ADDR` per
call instead of per `device_store`/`rate_counter` object). With that fixed, all five passed.

**Before counting that as "tested live," a broader independent sweep was run** - not just 5
hand-picked logins, but random realistic ones, through the actual Flask test client - because a
small hand-picked sample is exactly the kind of thing that looks fine by accident (see item 2c's
own F1-vs-live-traffic gap). First pass: 60 random logins across `uog.edu.pk`/`gmail.com`/
`yahoo.com`/`hotmail.com`/`outlook.com` - 24/60 (40%) false positives. Investigated before
concluding anything: `backend/app/admin_routes.py` only allows `@uog.edu.pk` accounts
(`create_user()`, line ~77), so free-domain emails can't occur in this system and that part of the
sweep was unrealistic by construction.

Re-ran restricted to `@uog.edu.pk` only, but with realistic *local-part* variety instead of the
training generator's own narrow shape (see below) - dotted names (`hamza.chaudhry`), underscored
names with a number (`sana_khan77`), and roll-number-style locals matching the FYP proposal
document's own numbering convention (`23011556099`, cf. `23011556-099` in `FYP_Proposal.docx`).
Accounts in this system are admin-created (`admin_routes.py`'s `create_user()`), not self-chosen at
signup, so there's no fixed convention forcing any one shape - a real admin could plausibly use any
of these. Result: **16/40 (40%) correct, 60% false-positive rate** - worse than item 2d's
2.7%, and not explained by the gmail/yahoo domains already ruled out above.

Root cause, confirmed directly against the training data
(`training.simulate_attacks.build_dataset()`): `_student_email(i)` in `simulate_attacks.py`
generates every benign training email as one of 18 fixed first names plus a one-or-two-digit
index, all under a single hardcoded `DOMAIN = "uog.edu.pk"` - roughly 400 distinct shapes total.
item 2d fixed the *password* dimension's narrowness with a real 100,000-entry corpus, but never
touched the *email* dimension, which is generated by the same narrow, hand-rolled scheme that sank
the very first CSIC-based attempt (item 2b) - a model trained on one axis of narrow "shape" learns
that shape as "normal" regardless of how varied the other axis is, and real accounts whose
local-part falls outside that narrow range (a longer name, a dot, an underscore, an 11-digit roll
number) get flagged at a high rate purely for being differently-shaped, not for being attacks.

**Reverted before any commit** - `ids/ids/model_loader.py`, `ids/ids/detector.py`,
`ids/requirements.txt`, `ids/routes/detect.py`, `ids/tests/test_model_loader.py`, and
`ids/tests/test_detect_route.py` are all back to their exact pre-attempt (HEAD) state, confirmed
with `git diff` and a clean 50/50 test pass. `ids/r2_model.pkl` (the shipping copy) was deleted;
`ids/training/output/r2_model_v3.pkl` (gitignored) still exists locally for whoever picks this up
next. The two route-level regression tests this attempt wrote
(`test_realistic_benign_logins_are_not_flagged_by_the_model`, with the per-login-IP fix described
above, and `test_a_novel_sql_injection_shaped_password_is_flagged`) asserted specifically on the
new model's `"Anomalous Request (ML)"` label, so they were reverted along with the wiring rather
than left in asserting on a model no longer in use - nothing from this attempt was committed, so
re-derive them from this item's description (five logins, distinct `REMOTE_ADDR` per call via
`environ_overrides`; one SQL-injection-shaped password with the deterministic regex checks
monkeypatched out) once a model actually ships.

**Not shipped. What's needed before trying again**: enrich `_student_email()` (or replace it) in
`training/simulate_attacks.py` with realistic local-part diversity - varied lengths, dotted names,
underscored names, roll-number-style numerics - the same way `PASSWORD_CORPUS` fixed the password
dimension in item 2d, then retrain, then re-run both this item's sweep methodology (distinct IPs
per login, `@uog.edu.pk`-only, varied local-part shapes) and item 2d's own real-password-corpus
and novel-attack checks before considering shipping again. Given two different dimensions of the
same underlying failure mode have now shown up (CSIC's URL-shaped traffic in 2b, this narrow
email-shape in 2e), it's worth treating "is every input dimension realistically varied, not just
the one being actively improved" as a standing question for the next attempt, not a one-off fix.

### 2f. Fifth attempt: the email generator fixed, retrained, shipped again - this one holds up

`_student_email()` in `training/simulate_attacks.py` was rewritten per item 2e's closing
paragraph: instead of one fixed `{first_name}{small_index}@uog.edu.pk` pattern, it now picks
deterministically (seeded on the account index, so the same student always maps to the same
email across every event generator that references them - `is_new_device`/
`login_attempts_per_minute` need a stable identity) from five realistic shapes - dotted names
(`hamza.chaudhry`), underscored names with a number (`sana_khan77`), bare name plus a 1-3 digit
number (`ayesha458`), roll-number-style numerics matching the FYP proposal document's own
convention (`23012323192`, cf. `23011556-099`), and dotted names with a number. Domain stays
fixed to `uog.edu.pk` - confirmed in item 2e that `backend/app/admin_routes.py` only allows that
domain, so that part of the old generator was never the problem.

Retrained (Experiment B, simulated-only, 12 features): F1 = 0.8854 (`r2_model_v3.pkl`,
SHA-256 `ffbdb9ee6515b33b94a7b04ed1aa4ada0253f7b84f5d6024b6512c9f41433697`) - close to item 2d's
0.8972, not a meaningfully different number either way; as established repeatedly in this
document, this number is not what decides anything.

**Validated against the exact failure mode that sank item 2e**, at increasing rigor:
- 300 passwords uniformly sampled from the real corpus, paired with emails from account indices
  never used in training (2000+, so unseen accounts too) - **4.0% false-positive rate**, loaded
  directly against the model object (no Flask route yet at this point).
- Wired into production the same way as every prior attempt (hash-pinned `ids/r2_model.pkl`,
  allowlist re-derived via the discovery-unpickler technique - same 7 classes as every model
  this project has shipped so far), then re-tested **through the real Flask route**, because a
  model-level check isn't the same as the full `/detect` pipeline - item 2c's own lesson.
- First broad sweep (60 logins, `@uog.edu.pk` only, 5 trained shapes + deliberately
  out-of-distribution shapes like three-part dotted names and hyphenated names, but with a fixed
  hand-picked password list left over from item 2e's reproduction) - 28% false-positive rate.
  **Investigated rather than accepted**: swapping that fixed password list for
  `simulate_attacks._random_password()` (the actual training-time password distribution) on the
  identical email set dropped it to 4/60 (6.7%) - the hand-picked passwords from item 2e's
  investigation were themselves unrepresentative (unusually long, symbol-heavy), not a sign the
  email fix had failed.
- **The real acceptance test**: 200 logins, `@uog.edu.pk` only, passwords uniformly sampled from
  the real corpus (same distribution item 2d validated against, not the weighted-common one
  training uses), emails split across the 5 trained shapes and genuinely out-of-distribution
  shapes not in the generator at all - **3.5% false-positive rate (7/200)**, in the same range as
  item 2d's password-only 2.7%, now with email shape genuinely varied too. The 7 misses are spread
  across both trained and OOD shapes, not concentrated in either - no sign of a remaining
  systematic shape gap, just ordinary classifier margin error.
- Novel attacks and a brute-force burst re-verified through the real route (not just the model
  object): SQLi/XSS strings mostly caught by the existing deterministic regex before even
  reaching the model (expected, unchanged from before); a path-traversal string with no
  deterministic check ahead of it reached the model and was correctly flagged; a 6-attempt
  same-IP burst was flagged at 5/6 attempts, escalating as expected.

**Shipped**: `ids/model_loader.py`, `ids/ids/detector.py`, `ids/routes/detect.py`,
`ids/requirements.txt` (gdown removed), `ids/tests/test_model_loader.py`, and
`ids/tests/test_detect_route.py` all updated the same way as the reverted item 2e attempt, plus
two new regression tests permanently covering what 2e got wrong
(`test_realistic_benign_logins_are_not_flagged_by_the_model`,
`test_varied_realistic_uog_email_shapes_are_mostly_not_flagged` - the latter allows at most 1/7
flagged, loose enough not to be flaky, tight enough to catch a real regression back toward 2e's
40-60%). All 53 tests pass. **Committed and pushed** (`2f2ab0a`).

### 3. SHAP integration — done (item 2h)
See item 2h below for the full writeup. Summary: `ids/ids/explainer.py` computes the top-3 SHAP
feature contributions for any ML-flagged (`Anomalous Request (ML)`) prediction, `detect.py`
returns them as `"explanation"` in `/detect`'s response and `log_intrusion` stores them alongside
the alert, and `AdminSecurityDashboard.jsx` renders a "Why (top features)" column.

### 2h. SHAP integration, verified live

Built per item 3's own outline, nothing deviated from the plan except where reality disagreed
with the sketch:
- `ids/ids/explainer.py` - `shap.TreeExplainer(model)`, built lazily (once, on first real use,
  same fail-safe-not-fail-closed shape as `model_loader.py`'s own model-load try/except - a SHAP
  failure degrades to no explanation, never breaks `/detect`). `shap==0.54.1` pinned in
  `ids/requirements.txt` (plus its own `cloudpickle`/`slicer` dependencies) - that version's
  `shap_values()` returns one array shaped `(n_samples, n_features, n_classes)`, not the list-of-
  arrays shape older shap versions used; `_class_contributions()` handles both shapes explicitly
  (tested against synthetic arrays of each shape, not just whichever one happens to be installed).
- Only ever computed for the ML branch, confirmed deliberately against the proposal's own wording
  ("every **alert** shows the three features...", not every request) - `ids/ids/detector.py`'s
  new `explain()` is called from `routes/detect.py` only when `predict_attack()` already returned
  `ANOMALOUS_LABEL`. A BENIGN prediction, or one caught by the deterministic SQL/XSS/blacklist
  checks earlier in `detect()`, returns `"explanation": null` - those rule-based matches already
  name the specific pattern that caught them, which is its own explanation; computing SHAP for
  every benign request (the overwhelming majority of traffic) would be wasted work nobody reads.
- `ids/ids/logger.py::log_intrusion()` gained an `explanation=None` parameter, stored in a new
  `intrusion_logs.explanation` column (`JSONB`, `database/supabase_setup.sql`) - so "every alert
  shows the three features..." holds for the Security Dashboard's history, not only the one live
  `/detect` response that happened to trigger it.
- `frontend/Components/IDS/AdminSecurityDashboard.jsx` - a new "Why (top features)" table column
  (and PDF export column) rendering each of the three `{feature, contribution, value}` entries,
  `—` when there's nothing to show. **Verified live**: ran the actual Vite dev server
  (`.claude/launch.json`, new - reusable for any future frontend verification in this repo),
  navigated to `/adminpage/securitydashboard`, and stubbed `window.fetch` in the browser console
  to return realistic mock rows (one ML-flagged alert with a real explanation array, one
  SQL-Injection alert with `explanation: null`) - confirmed via the rendered page text that the ML
  row shows all three `feature: contribution (value: ...)` lines and the rule-based row shows `—`,
  with no console errors. Not tested against a real Supabase instance with the new column
  (none available here) - the column itself (`ADD COLUMN IF NOT EXISTS`) still needs
  `database/supabase_setup.sql` re-run against the real project before this is visible there.
- 7 new tests in `ids/tests/test_explainer.py` (both shap output shapes, sorting, graceful
  degradation on failure, degradation when unavailable) plus updated assertions in
  `test_detect_route.py` (every branch's `"explanation"` key checked - `None` for
  blacklist/SQL/XSS/BENIGN, a real 1-3-entry list for the one ML-flagged case). Full `ids/`
  suite: 61 tests, all passing.
- Sanity-checked end to end, not just via the test suite: a live `/detect` call against a
  brute-force burst (6 same-IP attempts) showed `login_attempts_per_minute`'s contribution
  actually growing with each attempt (0.25 → 0.49 → 0.49 → 0.49) and dominating the explanation
  once the burst was underway - a legible, plausible "why," not just three arbitrary numbers.

### 4. Keep the existing safety behavior
`model_loader.py`'s fallback ("model unavailable → rule-based detection only, service still
starts") must keep working with your new model too — don't remove that try/except, and make sure
your new model's absence/corruption still degrades gracefully rather than taking the whole IDS
service down. Confirmed working as of this session; `ids/tests/test_model_loader.py` has the test
coverage pattern to extend once you swap in a new file.

## Suggested order (status as of 2026-10-22)
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
6. **Shipped** (see item 2f) - the first two attempts (items 2b, 2e) were reverted before
   committing because the specific model each would have shipped was actively unsafe (100% then
   40-60% false-positive rate on realistic traffic). The third attempt fixed the root cause item
   2e found (the training simulator's email generator was too narrow a shape, same problem as
   2b one dimension over) and validated at 3.5% FPR on a 200-sample sweep spanning both the
   simulator's trained email shapes and deliberately out-of-distribution ones, alongside
   unregressed novel-attack and brute-force detection. Committed and pushed (`2f2ab0a`) - see
   item 2f for the full acceptance-test writeup.
7. **Done** (see item 2h) - SHAP wired into `/detect`'s response, stored with each logged alert,
   and rendered in the Security Dashboard, verified live against the real Vite dev server with
   stubbed data.

## Testing
`ids/tests/` now has **61 tests**: 8 for `model_loader.py`'s security gates, 31 for
`features.py` (`test_features.py`), 7 for the wired-in `/detect` route
(`test_detect_route.py` - including the two regression tests added in item 2f for the exact
failure modes items 2b and 2e found, and explanation-shape assertions added in item 2h), 8 for
`ids/ids/explainer.py` (`test_explainer.py` - item 2h), 7 for the attack simulator (`test_simulate_attacks.py` - reproducibility,
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
