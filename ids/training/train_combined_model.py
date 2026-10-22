"""Two honest experiments, run back to back, both reported regardless of outcome:

Experiment A - CSIC 2010 + self-simulated traffic, combined, on the same 10 features
train_model.py trains on (the 10 features both sources can support - see that module's and
simulate_attacks.py's docstrings for why login_attempts_per_minute/is_new_device are excluded
from CSIC rows). This tests whether adding realistic login-shaped examples (CSIC's "normal"
class is e-commerce URLs, structurally unlike an email/password pair) and more attack examples
raises F1 above the CSIC-only baseline (0.8847).

Experiment B - self-simulated traffic only, all 12 features
Only the simulated data has genuine timestamps/IPs/account identity, so only it can honestly
support login_attempts_per_minute/is_new_device as trained features. This is supplementary
evidence (does the full feature set actually help, when it's available?), not a claim that this
is the production-ready model - a model trained only on simulated traffic has never seen a real
attack's text, and simulated "normal" traffic is necessarily less varied than real usage.

Experiment B, shipped: `ids/r2_model.pkl` (see docs/handoff/R2_explainable_ids_plan.md item 2d
for the full validation story - a real public password corpus for benign examples, two
independent live-traffic re-tests, not just this held-out F1) is Experiment B's model, trained
here with the exact hyperparameters and data pipeline below - re-run this script to reproduce it
byte-for-byte (fixed RANDOM_STATE throughout the training/ package).

Run from ids/: venv\\Scripts\\python.exe training\\train_combined_model.py
"""
import hashlib
import os
import pickle
import sys

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # import ids.*

from training.simulate_attacks import build_dataset as build_simulated_dataset
from training.train_model import TRAINABLE_FEATURE_NAMES, RANDOM_STATE, TEST_SIZE
from training.train_model import build_dataset as build_csic_dataset

ALL_TWELVE_FEATURES = TRAINABLE_FEATURE_NAMES + ["login_attempts_per_minute", "is_new_device"]
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
EXPERIMENT_B_MODEL_PATH = os.path.join(OUTPUT_DIR, "r2_model_v3.pkl")

# Same regularized config train_model.py settled on for the CSIC-only model (see that script's
# own comment): an earlier, unconstrained config (500 trees, max_features=None,
# min_samples_leaf=1) scored marginally higher F1 but produced a 170MB file and was almost
# certainly overfitting - this smaller configuration generalizes at least as well in every
# comparison made across this project's training scripts, at a fraction of the size.
MODEL_KWARGS = dict(n_estimators=150, max_depth=20, min_samples_leaf=4,
                    random_state=RANDOM_STATE, n_jobs=-1, class_weight="balanced_subsample")


def _evaluate(X, y, feature_names, label):
    X_train, X_test, y_train, y_test = train_test_split(
        X[feature_names], y, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y)
    model = RandomForestClassifier(**MODEL_KWARGS)
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    f1 = f1_score(y_test, pred, pos_label=1)
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    print(f"Train: {len(X_train)}  Test (held out): {len(X_test)}  Features: {len(feature_names)}")
    print(f"F1 (Anomalous class): {f1:.4f}")
    print(classification_report(y_test, pred, target_names=["Normal", "Anomalous"]))
    print("Feature importances:")
    for name, imp in sorted(zip(feature_names, model.feature_importances_), key=lambda t: -t[1]):
        print(f"  {name:30s} {imp:.4f}")
    return model, f1


def experiment_a_combined():
    X_csic, y_csic = build_csic_dataset()
    X_sim, y_sim, kind_sim = build_simulated_dataset()

    X_combined = pd.concat([X_csic[TRAINABLE_FEATURE_NAMES], X_sim[TRAINABLE_FEATURE_NAMES]],
                           ignore_index=True)
    y_combined = pd.concat([y_csic, y_sim], ignore_index=True)
    print(f"\nCombined dataset: {len(X_csic)} CSIC rows + {len(X_sim)} simulated rows "
          f"= {len(X_combined)} total")
    return _evaluate(X_combined, y_combined, TRAINABLE_FEATURE_NAMES,
                     "EXPERIMENT A: CSIC 2010 + self-simulated, 10 shared features")


def experiment_b_simulated_only_all_features(save=False):
    X_sim, y_sim, kind_sim = build_simulated_dataset()
    model, f1 = _evaluate(X_sim, y_sim, ALL_TWELVE_FEATURES,
                          "EXPERIMENT B: self-simulated only, all 12 features "
                          "(the only honest way to include login_attempts_per_minute/is_new_device)")
    if save:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        payload = {"model": model, "features": ALL_TWELVE_FEATURES}
        with open(EXPERIMENT_B_MODEL_PATH, "wb") as f:
            pickle.dump(payload, f)
        sha256 = hashlib.sha256(open(EXPERIMENT_B_MODEL_PATH, "rb").read()).hexdigest()
        print(f"\nSaved: {EXPERIMENT_B_MODEL_PATH}")
        print(f"SHA-256: {sha256}")
    return model, f1


if __name__ == "__main__":
    _, f1_a = experiment_a_combined()
    _, f1_b = experiment_b_simulated_only_all_features(save=True)

    print(f"\n{'#' * 70}")
    print("SUMMARY (all numbers are real, on genuinely held-out test splits):")
    print(f"  CSIC 2010 only, 10 features (train_model.py baseline):        F1 = 0.8847")
    print(f"  Experiment A - CSIC + simulated, 10 features:                 F1 = {f1_a:.4f}")
    print(f"  Experiment B - simulated only, all 12 features:               F1 = {f1_b:.4f}")
    print("#" * 70)
