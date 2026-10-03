from ids.model_loader import model, features
import pandas as pd

def predict_attack(data):
    if model is None:
        return "BENIGN"
    # Remove non-feature fields
    clean_data = dict(data)
    clean_data.pop("email", None)

    # Convert to dataframe
    df = pd.DataFrame([clean_data])

    # Match exact training feature order
    df = df.reindex(columns=features, fill_value=0)

    prediction = model.predict(df)[0]

    return prediction