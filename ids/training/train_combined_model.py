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

Run from ids/: venv\\Scripts\\python.exe training\\train_combined_model.py
"""
import os
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


def _evaluate(X, y, feature_names, label):
    X_train, X_test, y_train, y_test = train_test_split(
        X[feature_names], y, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y)
    model = RandomForestClassifier(n_estimators=500, random_state=RANDOM_STATE, n_jobs=-1,
                                   class_weight="balanced_subsample", min_samples_leaf=1,
                                   max_features=None)
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


def experiment_b_simulated_only_all_features():
    X_sim, y_sim, kind_sim = build_simulated_dataset()
    return _evaluate(X_sim, y_sim, ALL_TWELVE_FEATURES,
                     "EXPERIMENT B: self-simulated only, all 12 features "
                     "(the only honest way to include login_attempts_per_minute/is_new_device)")


if __name__ == "__main__":
    _, f1_a = experiment_a_combined()
    _, f1_b = experiment_b_simulated_only_all_features()

    print(f"\n{'#' * 70}")
    print("SUMMARY (all numbers are real, on genuinely held-out test splits):")
    print(f"  CSIC 2010 only, 10 features (train_model.py baseline):        F1 = 0.8847")
    print(f"  Experiment A - CSIC + simulated, 10 features:                 F1 = {f1_a:.4f}")
    print(f"  Experiment B - simulated only, all 12 features:               F1 = {f1_b:.4f}")
    print("#" * 70)
