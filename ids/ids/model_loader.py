import hashlib
import os
import pickle

import gdown

MODEL_PATH = "ids/ids_multiclass_model.pkl"
FILE_ID = "1AgrCG1C1XDRY-uy7asVIMutZ5WuOOybe"

# Pinned the moment this fix landed, over the exact bytes already used in production (a
# RandomForestClassifier trained on CICIDS2017-style flow features). If the file is ever
# legitimately replaced, verify the new file's provenance first, then update this hash and
# FILE_ID together - a mismatch is treated as a compromised/corrupted download, not ignored.
EXPECTED_SHA256 = "860669557039dd142b0219a15adb271e2a23248cdcc30cd738de69fbd6226890"

# Every class pickle.load() is allowed to construct while loading this file, discovered by
# statically walking the pickle's own GLOBAL/STACK_GLOBAL opcodes before ever trusting it (see
# the repo's security notes) - just RandomForestClassifier's own structure and the numpy arrays
# its trees are stored in. Nothing here can execute a shell command, import an arbitrary
# module, or run attacker-controlled code; anything not on this list is refused outright.
ALLOWED_CLASSES = {
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy._core.numeric", "_frombuffer"),
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
        url = f"https://drive.google.com/uc?id={FILE_ID}"
        gdown.download(url, MODEL_PATH, quiet=False)

    # Checked every load, not just right after a fresh download - a previously-downloaded file
    # sitting on disk could have been swapped or corrupted since, and skipping the check on the
    # "already have it" path would make this a one-time check instead of a standing guarantee.
    actual = _sha256_of(MODEL_PATH)
    if actual != EXPECTED_SHA256:
        raise ValueError(
            f"IDS model at {MODEL_PATH} does not match the pinned hash "
            f"(expected {EXPECTED_SHA256}, got {actual}) - refusing to load it. "
            f"Delete the file to force a fresh download, or update EXPECTED_SHA256 in "
            f"model_loader.py after verifying a legitimately new file's provenance.")

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
