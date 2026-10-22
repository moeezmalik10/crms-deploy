"""ids.explainer - SHAP explanations for R2's model (Component 9, Objective 6: "every alert
shows the three features that most influenced the decision"). See that module's own docstring
for why this is only ever computed for an Anomalous prediction, never a BENIGN one.
"""
import numpy as np
import pytest

from ids import explainer
from ids.detector import ANOMALOUS_LABEL, explain, predict_attack
from ids.features import extract_features, _InMemoryDeviceStore, _SlidingWindowCounter


def _features_for(email, password):
    return extract_features(email, password, "203.0.113.50", user_agent="pytest",
                            device_store=_InMemoryDeviceStore(), rate_counter=_SlidingWindowCounter(60))


# ---------------------------------------------------------------------------------------------
# _class_contributions: both of shap's historical output shapes, handled explicitly
# ---------------------------------------------------------------------------------------------

def test_class_contributions_handles_the_list_of_arrays_shape():
    """Older shap TreeExplainer versions: shap_values(X) returns a list, one (n_samples,
    n_features) array per class."""
    class_0 = np.array([[0.1, 0.2, 0.3]])
    class_1 = np.array([[0.4, 0.5, 0.6]])
    result = explainer._class_contributions([class_0, class_1], class_index=1)
    assert list(result) == [0.4, 0.5, 0.6]


def test_class_contributions_handles_the_trailing_class_axis_shape():
    """shap 0.54.1 (pinned in ids/requirements.txt): shap_values(X) returns one array, shape
    (n_samples, n_features, n_classes)."""
    arr = np.array([[[0.1, 0.4], [0.2, 0.5], [0.3, 0.6]]])  # (1 sample, 3 features, 2 classes)
    result = explainer._class_contributions(arr, class_index=1)
    assert list(result) == [0.4, 0.5, 0.6]


def test_class_contributions_handles_a_single_output_shape():
    """A regressor or single-output explainer with no class axis at all - shape (n_samples,
    n_features). class_index is irrelevant here; the function must not try to index a 1-D
    per-sample row as if it had a class axis."""
    arr = np.array([[0.1, 0.2, 0.3]])
    result = explainer._class_contributions(arr, class_index=1)
    assert list(result) == [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------------------------
# explain_prediction: against the real, committed model
# ---------------------------------------------------------------------------------------------

def test_explain_returns_at_most_three_features_with_the_right_shape():
    feats = _features_for("attacker@evil.com",
                          "1' AND SLEEP(5)-- -- UNION SELECT password FROM users WHERE 1=1#")
    result = explainer.explain_prediction(feats)
    assert 0 < len(result) <= 3
    for entry in result:
        assert set(entry.keys()) == {"feature", "contribution", "value"}
        assert entry["feature"] in feats
        assert entry["value"] == feats[entry["feature"]]
        assert isinstance(entry["contribution"], float)


def test_explain_is_sorted_by_absolute_contribution_descending():
    feats = _features_for("attacker@evil.com",
                          "1' AND SLEEP(5)-- -- UNION SELECT password FROM users WHERE 1=1#")
    result = explainer.explain_prediction(feats)
    magnitudes = [abs(e["contribution"]) for e in result]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_explain_degrades_to_empty_list_when_the_explainer_is_unavailable(monkeypatch):
    monkeypatch.setattr(explainer, "_get_explainer", lambda: None)
    feats = _features_for("a@uog.edu.pk", "whatever")
    assert explainer.explain_prediction(feats) == []


def test_explain_never_raises_even_if_shap_itself_blows_up(monkeypatch):
    class _ExplodingExplainer:
        def shap_values(self, df):
            raise RuntimeError("boom")

    monkeypatch.setattr(explainer, "_get_explainer", lambda: _ExplodingExplainer())
    feats = _features_for("a@uog.edu.pk", "whatever")
    assert explainer.explain_prediction(feats) == []


# ---------------------------------------------------------------------------------------------
# detector.explain(): the thin wrapper routes/detect.py actually calls
# ---------------------------------------------------------------------------------------------

def test_detector_explain_matches_an_anomalous_prediction():
    feats = _features_for("attacker@evil.com",
                          "1' AND SLEEP(5)-- -- UNION SELECT password FROM users WHERE 1=1#")
    assert predict_attack(feats) == ANOMALOUS_LABEL
    result = explain(feats)
    assert 0 < len(result) <= 3
