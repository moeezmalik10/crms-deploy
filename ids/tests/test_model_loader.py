"""model_loader.py downloads a pickle from a public Google Drive link and deserializes it -
pickle.load() on anything not fully trusted is a textbook RCE vector (a crafted __reduce__ can
run `os.system`/`subprocess.Popen`/etc. the instant it's deserialized). Two independent gates
now stand between that download and the running service: the file must match a pinned SHA-256
hash, and the unpickler itself refuses to construct any class outside a tight allowlist derived
from what the real model actually needs (sklearn's RandomForestClassifier/DecisionTreeClassifier/
Tree plus numpy's array-reconstruction helpers) - so even a hash-check bypass or a legitimate-
looking but tampered file still can't get arbitrary code to run during deserialization.

Module-level import of ids.model_loader is avoided at the top of this file (it would attempt a
real network download if ids/ids_multiclass_model.pkl isn't already present on disk, which is
both slow and not what any of these tests are actually about) - gdown.download is stubbed out
first, same effect as the "model unavailable" branch the service already falls back to today.
"""
import hashlib
import io
import os
import pickle
import sys
import types

import pytest

# Stub out gdown before model_loader's own module-level load attempt runs, so test collection
# never touches the network - mirrors the "model unavailable, rule-based only" fallback the
# service already has for exactly this situation (e.g. a small free server with no cached copy).
_fake_gdown = types.ModuleType("gdown")
_fake_gdown.download = lambda *a, **kw: None
sys.modules.setdefault("gdown", _fake_gdown)

from ids import model_loader  # noqa: E402


def test_module_level_import_falls_back_cleanly_with_no_cached_model():
    """With no local copy and gdown stubbed to a no-op, the module-level try/except must still
    leave the service in its documented degraded mode, not raise and crash the whole process."""
    assert model_loader.model is None or hasattr(model_loader.model, "predict")
    # The only way `model` is non-None here is if a real ids_multiclass_model.pkl happens to
    # already sit on disk from a previous run and passes its hash check - either way, importing
    # the module must never raise.


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
