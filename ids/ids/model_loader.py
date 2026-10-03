import pickle
import os
import gdown

MODEL_PATH = "ids/ids_multiclass_model.pkl"
FILE_ID = "1AgrCG1C1XDRY-uy7asVIMutZ5WuOOybe"

def load_model():
    if not os.path.exists(MODEL_PATH):
        url = f"https://drive.google.com/uc?id={FILE_ID}"
        gdown.download(url, MODEL_PATH, quiet=False)

    with open(MODEL_PATH, "rb") as f:
        data = pickle.load(f)

    model = data["model"]
    features = data["features"]

    return model, features

# Load once globally. If the model cannot be downloaded or loaded (for example on a small
# free server), the service still starts and the SQL-injection / XSS rules keep working.
try:
    model, features = load_model()
    print("IDS model loaded")
except Exception as e:
    print(f"WARNING: IDS model not available ({e}); running rule-based detection only")
    model, features = None, []