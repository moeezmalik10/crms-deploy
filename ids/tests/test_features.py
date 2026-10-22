"""ids.features - R2's request-level feature extraction (see that module's docstring for the
design decisions: login-attempt-rate as the "failed logins" proxy, SQL/XSS flags exposed as
features even though routes/detect.py's early return means they're usually 0 when the model
actually runs, and the in-memory device store as a documented starting point, not durable
tracking).
"""
from ids.features import (
    _InMemoryDeviceStore,
    _SlidingWindowCounter,
    _ip_subnet,
    device_fingerprint,
    digit_ratio,
    extract_features,
    has_path_traversal,
    has_sql_pattern,
    has_xss_pattern,
    is_new_device,
    login_attempts_per_minute,
    max_special_char_run,
    payload_length,
    special_character_count,
    sql_keyword_count,
    unusual_character_ratio,
    url_encoded_count,
)


# ---------------------------------------------------------------- payload_length
def test_payload_length_sums_both_fields():
    assert payload_length("a@b.com", "hunter2") == len("a@b.com") + len("hunter2")


def test_payload_length_handles_missing_fields():
    assert payload_length(None, None) == 0
    assert payload_length("a@b.com", None) == len("a@b.com")


# ---------------------------------------------------------------- unusual_character_ratio
def test_unusual_character_ratio_empty_string_is_zero():
    assert unusual_character_ratio("") == 0.0
    assert unusual_character_ratio(None) == 0.0


def test_unusual_character_ratio_ordinary_text_is_zero():
    assert unusual_character_ratio("student@uog.edu.pk") == 0.0


def test_unusual_character_ratio_all_unusual_is_one():
    assert unusual_character_ratio("\x00\x01\x02") == 1.0


def test_unusual_character_ratio_is_between_zero_and_one_for_mixed_text():
    r = unusual_character_ratio("hello\x00world")
    assert 0.0 < r < 1.0


# ---------------------------------------------------------------- has_sql_pattern / has_xss_pattern
def test_has_sql_pattern_detects_injection_in_either_field():
    assert has_sql_pattern("' OR 1=1 --", "x") == 1
    assert has_sql_pattern("x", "' OR 1=1 --") == 1
    assert has_sql_pattern("plain@uog.edu.pk", "normalpass") == 0


def test_has_xss_pattern_detects_script_in_either_field():
    assert has_xss_pattern("<script>alert(1)</script>", "x") == 1
    assert has_xss_pattern("x", "<img onerror=alert(1)>") == 1
    assert has_xss_pattern("plain@uog.edu.pk", "normalpass") == 0


def test_pattern_flags_return_int_not_bool():
    # A model/dataframe feature column should be numeric, not a Python bool subtype surprise.
    assert type(has_sql_pattern("x", "y")) is int
    assert type(has_xss_pattern("x", "y")) is int


# ---------------------------------------------------------------- sliding window / login rate
def test_sliding_window_counts_within_the_window():
    c = _SlidingWindowCounter(window_seconds=60)
    assert c.record("1.2.3.4", now=1000) == 1
    assert c.record("1.2.3.4", now=1010) == 2
    assert c.record("1.2.3.4", now=1020) == 3


def test_sliding_window_drops_hits_older_than_the_window():
    c = _SlidingWindowCounter(window_seconds=60)
    c.record("1.2.3.4", now=1000)
    c.record("1.2.3.4", now=1010)
    # 90s later - both earlier hits have aged out of a 60s window.
    assert c.record("1.2.3.4", now=1100) == 1


def test_sliding_window_keys_are_independent():
    c = _SlidingWindowCounter(window_seconds=60)
    c.record("1.2.3.4", now=1000)
    c.record("1.2.3.4", now=1001)
    assert c.record("5.6.7.8", now=1002) == 1   # unaffected by 1.2.3.4's two hits


def test_login_attempts_per_minute_uses_the_shared_module_counter():
    # Uses a unique key so this test can't collide with others sharing the module-level counter.
    assert login_attempts_per_minute("test-ip-unique-1", now=5000) == 1
    assert login_attempts_per_minute("test-ip-unique-1", now=5005) == 2


# ---------------------------------------------------------------- ip subnet / device fingerprint
def test_ip_subnet_ipv4_is_a_slash_24():
    assert _ip_subnet("10.20.23.1") == "10.20.23.0/24"
    assert _ip_subnet("10.20.23.254") == "10.20.23.0/24"   # same /24 as above


def test_ip_subnet_ipv6_is_a_slash_64():
    assert _ip_subnet("2001:db8::1").endswith("/64")


def test_ip_subnet_falls_back_to_the_raw_value_on_garbage_input():
    assert _ip_subnet("not-an-ip") == "not-an-ip"
    assert _ip_subnet("") == ""


def test_device_fingerprint_is_deterministic():
    a = device_fingerprint("student@uog.edu.pk", "Mozilla/5.0", "10.20.23.1")
    b = device_fingerprint("student@uog.edu.pk", "Mozilla/5.0", "10.20.23.1")
    assert a == b


def test_device_fingerprint_is_case_insensitive_on_email():
    a = device_fingerprint("Student@UoG.edu.pk", "Mozilla/5.0", "10.20.23.1")
    b = device_fingerprint("student@uog.edu.pk", "Mozilla/5.0", "10.20.23.1")
    assert a == b


def test_device_fingerprint_differs_for_a_different_account():
    a = device_fingerprint("student1@uog.edu.pk", "Mozilla/5.0", "10.20.23.1")
    b = device_fingerprint("student2@uog.edu.pk", "Mozilla/5.0", "10.20.23.1")
    assert a != b


def test_device_fingerprint_same_across_the_same_subnet():
    # .1 and .254 are both inside 10.20.23.0/24 - a DHCP reshuffle inside the same lab shouldn't
    # look like a brand new device.
    a = device_fingerprint("student@uog.edu.pk", "Mozilla/5.0", "10.20.23.1")
    b = device_fingerprint("student@uog.edu.pk", "Mozilla/5.0", "10.20.23.254")
    assert a == b


# ---------------------------------------------------------------- is_new_device
def test_is_new_device_true_the_first_time_false_the_second():
    store = _InMemoryDeviceStore()
    assert is_new_device("s@uog.edu.pk", "UA", "10.0.0.1", device_store=store) == 1
    assert is_new_device("s@uog.edu.pk", "UA", "10.0.0.1", device_store=store) == 0


def test_is_new_device_independent_per_account():
    store = _InMemoryDeviceStore()
    assert is_new_device("s1@uog.edu.pk", "UA", "10.0.0.1", device_store=store) == 1
    assert is_new_device("s2@uog.edu.pk", "UA", "10.0.0.1", device_store=store) == 1


# ---------------------------------------------------------------- extract_features
def test_extract_features_returns_every_feature_with_the_right_keys():
    store = _InMemoryDeviceStore()
    out = extract_features("s@uog.edu.pk", "pw", "10.0.0.5", user_agent="UA",
                           now=9000, device_store=store)
    assert set(out.keys()) == {
        "payload_length", "unusual_character_ratio", "has_sql_pattern", "has_xss_pattern",
        "special_character_count", "max_special_char_run", "url_encoded_count",
        "has_path_traversal", "sql_keyword_count", "digit_ratio",
        "login_attempts_per_minute", "is_new_device",
    }
    assert out["payload_length"] == payload_length("s@uog.edu.pk", "pw")
    assert out["is_new_device"] == 1   # first time seeing this fingerprint in a fresh store


# ---------------------------------------------------------------- the richer, granular features
# Added after a first training pass (ids/training/train_model.py) on CSIC 2010 showed the
# original four features plateau around F1 0.84 - see features.py's own comment above these.

def test_special_character_count_counts_unusual_chars_not_just_their_ratio():
    assert special_character_count("plain@uog.edu.pk", "normalpass") == 0
    assert special_character_count("x", "\x00\x01\x02") == 3


def test_max_special_char_run_finds_the_longest_consecutive_stretch():
    assert max_special_char_run("plain@uog.edu.pk", "normal") == 0
    # three ordinary chars, then a run of 5 unusual ones, then ordinary again
    assert max_special_char_run("abc\x00\x00\x00\x00\x00def", "") == 5


def test_max_special_char_run_is_not_fooled_by_scattered_unusual_chars():
    # same total unusual-char count as the run above, but spread out - the run is much shorter
    text = "a\x00b\x00c\x00d\x00e\x00f"
    assert max_special_char_run(text, "") == 1


def test_url_encoded_count_counts_percent_hex_sequences():
    assert url_encoded_count("normal@uog.edu.pk", "normalpass") == 0
    assert url_encoded_count("%27%20OR%201=1", "") == 3


def test_has_path_traversal_detects_dot_dot_slash():
    assert has_path_traversal("../../etc/passwd", "") == 1
    assert has_path_traversal("..\\..\\windows\\system32", "") == 1
    assert has_path_traversal("normal@uog.edu.pk", "normalpass") == 0


def test_has_path_traversal_detects_the_url_encoded_form():
    assert has_path_traversal("%2e%2e%2f%2e%2e%2f", "") == 1


def test_sql_keyword_count_counts_every_keyword_not_just_whether_any_matched():
    assert sql_keyword_count("plain@uog.edu.pk", "normalpass") == 0
    assert sql_keyword_count("' UNION SELECT user,pass FROM users WHERE 1=1 --", "") > 1


def test_digit_ratio_between_zero_and_one():
    assert digit_ratio("", "") == 0.0
    assert digit_ratio("abc", "def") == 0.0
    assert digit_ratio("123", "456") == 1.0
    r = digit_ratio("abc123", "")
    assert 0.0 < r < 1.0
