"""SHAP-based explanation for R2's model predictions (Component 9, Objective 6: "every alert
shows the three features that most influenced the decision").

Only ever meaningful for a prediction the model itself flagged as Anomalous - a BENIGN
prediction has nothing alert-worthy to explain, and computing SHAP values for the overwhelming
majority of (benign) traffic would be wasted work with no caller that reads the result. See
ids/routes/detect.py, which only calls explain_prediction() when the ML branch actually fires.

shap.TreeExplainer is used (not a generic, model-agnostic Explainer) because
RandomForestClassifier has an exact, fast path through the tree structure itself - no sampling or
approximation the way a model-agnostic explainer needs, and no "background data" argument to
choose.
"""
import pandas as pd

from ids.model_loader import model, features

_explainer = None
_explainer_init_tried = False


def _get_explainer():
    """Built lazily, once, the first time an explanation is actually needed - not at import
    time, so a SHAP import/build failure degrades to "no explanation" rather than taking the
    whole service down, the same fail-safe-not-fail-closed philosophy as model_loader.py's own
    model-load failure."""
    global _explainer, _explainer_init_tried
    if _explainer is not None or _explainer_init_tried:
        return _explainer
    _explainer_init_tried = True
    if model is None:
        return None
    try:
        import shap
        _explainer = shap.TreeExplainer(model)
    except Exception as e:
        print(f"WARNING: SHAP explainer unavailable ({e}); predictions will have no 'why'")
    return _explainer


def _class_contributions(shap_values, class_index):
    """Normalizes across shap's two historical output shapes for a classifier's
    Explainer.shap_values(): a list of one array per class (older TreeExplainer versions), or a
    single array with a trailing class axis, shape (n_samples, n_features, n_classes) (shap
    0.54.1, the version this was built and tested against - see ids/requirements.txt). Returns
    the per-feature contribution array for one sample, one class."""
    if isinstance(shap_values, list):
        return shap_values[class_index][0]
    arr = shap_values[0]
    return arr[:, class_index] if arr.ndim == 2 else arr


def explain_prediction(computed_features, predicted_class=1, top_n=3):
    """The top `top_n` features (by absolute SHAP contribution toward `predicted_class`) for one
    already-computed feature dict - the same reindex-by-trained-column-order contract as
    ids.detector.predict_attack, so the explanation is always computed against the exact feature
    vector the model actually saw, not a differently-ordered reconstruction of it. Returns []
    if SHAP isn't available or anything about computing it fails (never raises) - an explanation
    is a nice-to-have on top of a prediction that already happened, not something that should
    ever block or break /detect."""
    explainer = _get_explainer()
    if explainer is None:
        return []
    try:
        df = pd.DataFrame([computed_features]).reindex(columns=features, fill_value=0)
        shap_values = explainer.shap_values(df)
        class_index = list(model.classes_).index(predicted_class)
        contributions = _class_contributions(shap_values, class_index)
        pairs = sorted(zip(features, contributions), key=lambda p: -abs(p[1]))[:top_n]
        return [
            {"feature": name, "contribution": round(float(value), 4),
             "value": computed_features.get(name)}
            for name, value in pairs
        ]
    except Exception as e:
        print(f"WARNING: SHAP explanation failed ({e})")
        return []
