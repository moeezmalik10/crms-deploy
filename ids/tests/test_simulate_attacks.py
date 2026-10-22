"""training/simulate_attacks.py - unlike train_model.py (needs the 29MB CSIC CSV, not in CI),
this is pure synthetic generation plus real extract_features() calls, so it's fast and safe to
run in the normal test suite. These tests exist to catch exactly the kind of mistake the R2
handoff doc's own results section warns about: a simulator where "new device" alone leaks into
the attack label, or where the benign/attack classes are trivially separable by construction.
"""
from training.simulate_attacks import build_dataset, generate_events


def test_generation_is_reproducible():
    """A fixed RANDOM_SEED means the same events every run - without this, a reported F1 number
    couldn't be reproduced by anyone re-running the script."""
    a = generate_events()
    b = generate_events()
    assert [e["email"] for e in a] == [e["email"] for e in b]
    assert [e["label"] for e in a] == [e["label"] for e in b]
    assert [e["t"] for e in a] == [e["t"] for e in b]


def test_events_are_in_chronological_order():
    """login_attempts_per_minute/is_new_device are stateful and order-sensitive - feeding them
    out of time order would compute nonsense rates."""
    events = generate_events()
    times = [e["t"] for e in events]
    assert times == sorted(times)


def test_both_classes_are_present_and_reasonably_balanced():
    X, y, kind = build_dataset()
    counts = y.value_counts()
    assert counts[0] > 0 and counts[1] > 0
    ratio = counts[0] / counts[1]
    assert 0.5 < ratio < 2.0   # not wildly imbalanced either way


def test_new_device_alone_is_not_an_attack_signal():
    """The documented design constraint (see simulate_attacks.py's module docstring): a benign
    first-ever login is new-device and still Normal. If this ever started failing, it would mean
    the generator started teaching the model "new device = attack", which is false and exactly
    the mistake the docstring says to avoid."""
    X, y, kind = build_dataset()
    normal_new_device = ((y == 0) & (X["is_new_device"] == 1)).sum()
    assert normal_new_device > 0, "no benign event is ever a first-time device - unrealistic and untested"


def test_login_attempts_per_minute_is_not_a_perfect_separator():
    """A deliberate anti-"too easy" check: some benign events should still show a burst (the
    quick-retry scenario), and some attack events should still show rate==1 (a single injection
    attempt, no burst) - otherwise this one feature trivially solves the whole problem and the
    simulation isn't testing anything realistic."""
    X, y, kind = build_dataset()
    assert (X.loc[y == 0, "login_attempts_per_minute"] > 1).any(), "no benign bursts - too easy"
    assert (X.loc[y == 1, "login_attempts_per_minute"] == 1).any(), "every attack bursts - too easy"


def test_every_attack_kind_and_every_benign_kind_is_actually_generated():
    X, y, kind = build_dataset()
    kinds_present = set(kind.unique())
    assert {"benign_normal", "benign_typo", "benign_quick_retry",
            "attack_injection", "attack_bruteforce", "attack_takeover_combo"} <= kinds_present


def test_build_dataset_features_match_extract_features_keys():
    from ids.features import extract_features
    X, y, kind = build_dataset()
    expected = set(extract_features("a@b.com", "x", "1.2.3.4", user_agent="UA", now=0).keys())
    assert set(X.columns) == expected
