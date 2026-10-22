import pandas as pd

from ids.model_loader import model, features
from ids.explainer import explain_prediction

# The model (see model_loader.py's own comment for which one and why) was trained on 0/1 labels
# (1 = Anomalous - see ids/training/simulate_attacks.py/train_model.py), not label strings. Map
# back to the same label strings the SQL/XSS/blacklist checks in routes/detect.py already return,
# so callers (and the frontend) only ever see one label vocabulary regardless of which check
# caught the request.
LABELS = {0: "BENIGN", 1: "Anomalous Request (ML)"}
ANOMALOUS_LABEL = LABELS[1]


def predict_attack(computed_features):
    """computed_features: the dict from ids.features.extract_features() - already has exactly
    the keys this model was trained on. reindex (rather than trusting dict order) is what
    actually enforces the model's training-time column order; the dict's own key order is just
    for readability."""
    if model is None:
        return "BENIGN"

    df = pd.DataFrame([computed_features]).reindex(columns=features, fill_value=0)
    prediction = int(model.predict(df)[0])

    return LABELS.get(prediction, "BENIGN")


def explain(computed_features):
    """The top-3 SHAP feature contributions behind an Anomalous prediction - see
    ids.explainer's own docstring for why this is only ever meaningful (and only ever worth
    calling) after predict_attack(computed_features) returned ANOMALOUS_LABEL, not for a BENIGN
    one."""
    return explain_prediction(computed_features, predicted_class=1)
