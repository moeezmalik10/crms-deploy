import hashlib
import os
import pickle

# R2's explainable, request-level intrusion-detection model (see
# docs/handoff/R2_explainable_ids_plan.md item 2f for how this specific file was trained and
# validated - it replaces a model trained on CICIDS2017-style network-flow features that
# detector.py fed with data /detect never actually has, so every real feature it used was
# always 0). Committed directly into the repo (3.7MB) instead of downloaded from an external
# host at startup - the hash check below is defense-in-depth against corruption, not the only
# thing standing between this file and whatever wrote it.
MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "r2_model.pkl")

# Pinned over ids/training/train_combined_model.py's actual output (fixed random_state
# throughout the training/ package - re-running that script reproduces this exact file and hash
# byte-for-byte). If the model is ever legitimately retrained and replaced, update this hash to
# match the new file - a mismatch is treated as a corrupted or tampered file, not ignored.
EXPECTED_SHA256 = "ffbdb9ee6515b33b94a7b04ed1aa4ada0253f7b84f5d6024b6512c9f41433697"

# Every class pickle.load() is allowed to construct while loading this file, discovered by
# statically walking the pickle's own GLOBAL/STACK_GLOBAL opcodes before ever trusting it (same
# discovery-unpickler technique used for the previous model - see git history). Just
# RandomForestClassifier's own structure and the numpy arrays its trees are stored in. Nothing
# here can execute a shell command, import an arbitrary module, or run attacker-controlled code;
# anything not on this list is refused outright.
ALLOWED_CLASSES = {
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("sklearn.ensemble._forest", "RandomForestClassifier"),
    ("sklearn.tree._classes", "DecisionTreeClassifier"),
    ("sklearn.tree._tree", "Tree"),
}


class RestrictedUnpickler(pickle.Unpickler):
    """Refuses to construct anything outside ALLOWED_CLASSES. A stock pickle.load() runs
    whatever __reduce__/__setstate__ the stream asks for - including, classically,
    `os.system` or `subprocess.Popen` - the moment it's deserialized, with no code of ours
    ever running in between to object. Overriding find_class() is pickle's own documented
    mechanism for closing that off (docs.python.org/3/library/pickle.html#restricting-globals):
    it is the only place pickle resolves an externally-referenced name, so this is a hard gate,
    not a best-effort filter."""

    def find_class(self, module, name):
        if (module, name) not in ALLOWED_CLASSES:
            raise pickle.UnpicklingError(
                f"refusing to unpickle disallowed class {module}.{name} "
                f"(not in model_loader.ALLOWED_CLASSES)")
        return super().find_class(module, name)


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_model():
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(
            f"{MODEL_PATH} not found - it should be committed in the repo (see this module's "
            f"own comment); if it's genuinely missing, re-run "
            f"ids/training/train_combined_model.py and commit ids/r2_model.pkl.")

    actual = _sha256_of(MODEL_PATH)
    if actual != EXPECTED_SHA256:
        raise ValueError(
            f"IDS model at {MODEL_PATH} does not match the pinned hash "
            f"(expected {EXPECTED_SHA256}, got {actual}) - refusing to load it. "
            f"If this is a legitimate retrain, verify its provenance first, then update "
            f"EXPECTED_SHA256 in model_loader.py to match.")

    with open(MODEL_PATH, "rb") as f:
        data = RestrictedUnpickler(f).load()

    model = data["model"]
    features = data["features"]

    return model, features


# Load once globally. If the model cannot be downloaded, fails its hash check, or cannot be
# loaded (for example on a small free server), the service still starts and the SQL-injection /
# XSS rules keep working.
try:
    model, features = load_model()
    print("IDS model loaded")
except Exception as e:
    print(f"WARNING: IDS model not available ({e}); running rule-based detection only")
    model, features = None, []
