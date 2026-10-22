import pandas as pd

from ids.model_loader import model, features

# The model (see model_loader.py's own comment for which one and why) was trained on 0/1 labels
# (1 = Anomalous - see ids/training/simulate_attacks.py/train_model.py), not label strings. Map
# back to the same label strings the SQL/XSS/blacklist checks in routes/detect.py already return,
# so callers (and the frontend) only ever see one label vocabulary regardless of which check
# caught the request.
LABELS = {0: "BENIGN", 1: "Anomalous Request (ML)"}


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
