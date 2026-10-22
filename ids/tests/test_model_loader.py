"""model_loader.py loads a pickle committed in the repo (ids/r2_model.pkl) and deserializes it -
pickle.load() on anything not fully trusted is a textbook RCE vector (a crafted __reduce__ can
run `os.system`/`subprocess.Popen`/etc. the instant it's deserialized). Two independent gates
stand between that file and the running service: it must match a pinned SHA-256 hash, and the
unpickler itself refuses to construct any class outside a tight allowlist derived from what the
real model actually needs (sklearn's RandomForestClassifier/DecisionTreeClassifier/Tree plus
numpy's array-reconstruction helpers) - so even a hash-check bypass or a legitimate-looking but
tampered file still can't get arbitrary code to run during deserialization.
"""
import hashlib
import io
import os
import pickle

import pytest

from ids import model_loader


def test_module_level_import_loads_the_committed_model():
    """The real ids/r2_model.pkl is committed in the repo and must load cleanly and pass its
    own hash check at import time - if this ever fails, the service silently falls back to
    rule-based-only detection in production, which is worth a loud test failure, not a shrug."""
    assert model_loader.model is not None
    assert hasattr(model_loader.model, "predict")
    assert model_loader.features == [
        "payload_length", "unusual_character_ratio", "has_sql_pattern", "has_xss_pattern",
        "special_character_count", "max_special_char_run", "url_encoded_count",
        "has_path_traversal", "sql_keyword_count", "digit_ratio",
        "login_attempts_per_minute", "is_new_device",
    ]


def test_sha256_of_matches_hashlib_directly(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world" * 1000)
    assert model_loader._sha256_of(str(p)) == hashlib.sha256(b"hello world" * 1000).hexdigest()


def test_load_model_rejects_a_file_that_does_not_match_the_pinned_hash(tmp_path, monkeypatch):
    fake = tmp_path / "fake_model.pkl"
    fake.write_bytes(pickle.dumps({"model": "not the real model", "features": []}))
    monkeypatch.setattr(model_loader, "MODEL_PATH", str(fake))

    with pytest.raises(ValueError, match="does not match the pinned hash"):
        model_loader.load_model()


def test_load_model_accepts_a_file_whose_hash_was_pinned_for_it(tmp_path, monkeypatch):
    """The hash check itself isn't hardcoded to reject everything - a file that genuinely
    matches EXPECTED_SHA256 (and only contains allowed classes) loads normally."""
    import numpy as np

    payload = pickle.dumps({"model": np.array([1, 2, 3]), "features": ["a", "b"]})
    fake = tmp_path / "fake_model.pkl"
    fake.write_bytes(payload)
    monkeypatch.setattr(model_loader, "MODEL_PATH", str(fake))
    monkeypatch.setattr(model_loader, "EXPECTED_SHA256", hashlib.sha256(payload).hexdigest())

    model, features = model_loader.load_model()
    assert list(model) == [1, 2, 3]
    assert features == ["a", "b"]


class _EvilReduce:
    """Stands in for a crafted pickle payload: when deserialized, __reduce__ tells pickle to
    call os.system with an attacker-chosen command - the classic pickle RCE primitive."""

    def __reduce__(self):
        return (os.system, ("echo pwned > should_not_exist.txt",))


def test_restricted_unpickler_refuses_a_real_os_system_payload(tmp_path):
    payload = pickle.dumps({"model": _EvilReduce(), "features": []})
    marker = tmp_path / "should_not_exist.txt"
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(pickle.UnpicklingError, match="refusing to unpickle disallowed class"):
            model_loader.RestrictedUnpickler(io.BytesIO(payload)).load()
        assert not marker.exists(), "the os.system payload actually ran - RCE is NOT blocked"
    finally:
        os.chdir(cwd)


def test_restricted_unpickler_refuses_arbitrary_builtins_eval():
    class EvilEval:
        def __reduce__(self):
            return (eval, ("__import__('os').getcwd()",))

    payload = pickle.dumps(EvilEval())
    with pytest.raises(pickle.UnpicklingError, match="refusing to unpickle disallowed class"):
        model_loader.RestrictedUnpickler(io.BytesIO(payload)).load()


def test_restricted_unpickler_allows_a_plain_numpy_array():
    import numpy as np

    payload = pickle.dumps(np.array([1.0, 2.0, 3.0]))
    arr = model_loader.RestrictedUnpickler(io.BytesIO(payload)).load()
    assert list(arr) == [1.0, 2.0, 3.0]


def test_allowed_classes_contains_no_dangerous_modules():
    """A coarse sanity net on the allowlist itself: nothing in it should ever come from a
    module capable of running shell commands or arbitrary code, no matter what gets added to
    the model format in the future."""
    dangerous_modules = {"os", "nt", "posix", "subprocess", "builtins", "sys", "shutil",
                         "pickle", "importlib", "runpy", "code", "codeop"}
    for module, name in model_loader.ALLOWED_CLASSES:
        top_level = module.split(".")[0]
        assert top_level not in dangerous_modules, f"{module}.{name} looks dangerous to allow"
