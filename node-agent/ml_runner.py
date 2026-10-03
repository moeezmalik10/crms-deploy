import sys
import json
import warnings
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

from sklearn.model_selection import train_test_split, KFold, StratifiedKFold
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    mean_squared_error, r2_score
)
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.feature_extraction.text import TfidfVectorizer
from scipy.sparse import hstack
import scipy.sparse as sp

# =========================
# INPUT ARGS
# =========================
task_id         = sys.argv[1]
model_type      = sys.argv[2]   # "linear_regression" or "logistic_regression"
validation_type = sys.argv[3]   # "train_test_split" or "kfold"
start_row       = int(sys.argv[4])
end_row         = int(sys.argv[5])
data_file       = sys.argv[6]
fed_file        = sys.argv[7]

is_regression = (model_type == "linear_regression")

# =========================
# FAIL HANDLER
# =========================
def fail_output(reason, fed_round=1):
    print(f"[ML RUNNER] FAILED: {reason}", file=sys.stderr)
    print(json.dumps({
        "task_id":     task_id,
        "model":       model_type,
        "metrics":     {},
        "range":       f"{start_row}-{end_row}",
        "status":      "failed",
        "explanation": {},
        "weights":     None,
        "intercept":   None,
        "round":       fed_round,
        "error":       reason
    }))
    sys.exit(0)

# =========================
# LOAD DATA
# =========================
try:
    df = pd.read_json(data_file)
except Exception as e:
    fail_output(f"Data loading failed: {e}")

print(f"[ML RUNNER] Rows: {len(df)} | Columns: {list(df.columns)}", file=sys.stderr)

if df.shape[1] < 2:
    fail_output("Dataset must have at least 2 columns (features + target)")

# =========================
# FEDERATED PARAMS
# =========================
try:
    with open(fed_file, "r") as f:
        fed_params = json.load(f)
except Exception:
    fed_params = {}

fed_round        = fed_params.get("round", 1)
global_weights   = fed_params.get("global_weights", None)
global_intercept = fed_params.get("global_intercept", None)

print(f"[ML RUNNER] Round: {fed_round} | Model: {model_type} | Validation: {validation_type}", file=sys.stderr)

# =========================
# SPLIT FEATURES / TARGET
# =========================
X_raw = df.iloc[:, :-1].copy()
y     = df.iloc[:, -1].copy()

print(f"[ML RUNNER] Features: {list(X_raw.columns)} | Target: {y.name}", file=sys.stderr)

# =========================
# FEATURE ENGINEERING
# Handles: numeric, boolean, datetime, categorical, free text (TF-IDF)
# =========================
numeric_parts = []
sparse_parts  = []

for col in X_raw.columns:
    series = X_raw[col]

    # Skip all-null columns
    if series.isnull().all():
        print(f"[ML RUNNER] Dropping all-null column: '{col}'", file=sys.stderr)
        continue

    dtype = str(series.dtype)

    # Boolean → 0/1 astype(int) converts True→1, False→0. Models understand 1/0, not True/False.
    if series.dtype == bool:
        numeric_parts.append(series.astype(int).rename(col))

    # Already numeric
    elif series.dtype in [np.float64, np.float32, np.int64, np.int32, np.int16, np.uint8]:
        numeric_parts.append(series.rename(col))

    # Datetime → year, month, day
    elif "datetime" in dtype:
        dt = pd.to_datetime(series, errors="coerce")
        numeric_parts.append(dt.dt.year.fillna(0).astype(int).rename(f"{col}_year"))
        numeric_parts.append(dt.dt.month.fillna(0).astype(int).rename(f"{col}_month"))
        numeric_parts.append(dt.dt.day.fillna(0).astype(int).rename(f"{col}_day"))
        print(f"[ML RUNNER] Datetime '{col}' → year/month/day", file=sys.stderr)

    # String / Object / Category
    elif dtype in ["object", "string", "category"] or "object" in dtype:

        # Try numeric parse first (e.g. "1.5", "100")
        numeric_attempt = pd.to_numeric(series, errors="coerce")
        if numeric_attempt.notna().mean() > 0.8:
            numeric_parts.append(numeric_attempt.fillna(numeric_attempt.median()).rename(col))
            print(f"[ML RUNNER] '{col}' parsed as numeric", file=sys.stderr)

        else:
            n_unique = series.nunique()
            ratio    = n_unique / max(len(series), 1)

            # High cardinality / free text → TF-IDF
            if ratio > 0.3 or n_unique > 100:
                try:
                    tfidf = TfidfVectorizer(
                        max_features=200,
                        strip_accents="unicode",
                        lowercase=True,
                        stop_words="english",
                        ngram_range=(1, 2) #ngram_range=(1,2) means the model uses both unigrams (1 word) and bigrams (2 words) to represent text.
                    )
                    sparse_mat = tfidf.fit_transform(series.fillna("").astype(str))
                    sparse_parts.append(sparse_mat)
                    print(f"[ML RUNNER] TF-IDF '{col}' → {sparse_mat.shape[1]} features", file=sys.stderr)
                except Exception as e:
                    print(f"[ML RUNNER] TF-IDF failed for '{col}': {e} — skipping", file=sys.stderr)

            # Low cardinality → Label encode
            else:
                try:
                    encoded = LabelEncoder().fit_transform(series.fillna("missing").astype(str))
                    numeric_parts.append(pd.Series(encoded, name=col))
                    print(f"[ML RUNNER] Label encoded '{col}' ({n_unique} categories)", file=sys.stderr)
                except Exception as e:
                    print(f"[ML RUNNER] Could not encode '{col}': {e} — skipping", file=sys.stderr)

    # Unknown dtype → try numeric
    else:
        try:
            numeric_parts.append(pd.to_numeric(series, errors="coerce").fillna(0).rename(col))
        except Exception:
            print(f"[ML RUNNER] Skipping unknown column '{col}'", file=sys.stderr)

# Build final feature matrix
if numeric_parts:
    X_numeric = pd.concat(numeric_parts, axis=1).reset_index(drop=True)
    X_numeric = pd.DataFrame(
        SimpleImputer(strategy="mean").fit_transform(X_numeric),
        columns=X_numeric.columns
    )
    X_scaled = StandardScaler().fit_transform(X_numeric)
    X_num_sp = sp.csr_matrix(X_scaled)
else:
    X_num_sp = None

all_parts = ([X_num_sp] if X_num_sp is not None else []) + sparse_parts

if not all_parts:
    fail_output("No usable feature columns after processing.", fed_round)

X_final = hstack(all_parts).tocsr() if len(all_parts) > 1 else all_parts[0]

print(f"[ML RUNNER] Feature matrix: {X_final.shape[0]} rows × {X_final.shape[1]} cols", file=sys.stderr)

# =========================
# TARGET CLEANING
# =========================
# Try numeric conversion first
y_numeric = pd.to_numeric(y, errors="coerce")

if y_numeric.notna().mean() > 0.8:
    # Mostly numeric — use as is
    y = y_numeric.fillna(y_numeric.median())
else:
    # String target — label encode regardless of model type
    # This converts 'Atoka County, Oklahoma' → 0, 1, 2... etc.
    # For regression: encoded integers become numeric target
    # For classification: encoded integers become class labels
    y = pd.Series(LabelEncoder().fit_transform(y.astype(str).fillna("missing")))
    print(f"[ML RUNNER] Target label-encoded (was string, {y.nunique()} unique values)", file=sys.stderr)

y = y.reset_index(drop=True)

unique_classes = y.nunique()
unique_vals    = sorted(y.unique()[:20])# first 20 uniques target classes for display
print(f"[ML RUNNER] Target unique values: {unique_classes}", file=sys.stderr)

# =========================
# AUTO-DETECT: switch model if needed
# logistic_regression on continuous y → switch to linear_regression
# =========================
if not is_regression:
    is_float_continuous = (
        y.dtype in [np.float64, np.float32] and
        not all(float(v).is_integer() for v in unique_vals)
    )
    if unique_classes > 20 or is_float_continuous:
        print(f"[ML RUNNER] Target looks continuous ({unique_classes} unique) — switching to linear_regression", file=sys.stderr)
        model_type    = "linear_regression"
        is_regression = True

# For regression on encoded string target — inform user
if is_regression and unique_classes > 50:
    print(f"[ML RUNNER] Regression on encoded string target ({unique_classes} unique values)", file=sys.stderr)

# Classification needs at least 2 classes
if not is_regression and unique_classes < 2:
    fail_output(
        f"Target has only {unique_classes} unique class in chunk {start_row}→{end_row}. "
        f"Cannot train classifier.",
        fed_round
    )

# Ensure integer labels for classification
if not is_regression:
    y = y.round().astype(int)

print(f"[ML RUNNER] Task: {'regression' if is_regression else 'classification'}", file=sys.stderr)


# =========================
# MODEL
# =========================
def get_model():
    if is_regression:
        return LinearRegression()
    else:
        return LogisticRegression(max_iter=2000, solver="saga", C=1.0)

model = get_model()

# =========================
# FEDERATED WEIGHT INJECTION (ROUND 2)
# =========================
def inject_global_weights(model, global_weights, global_intercept, n_features):
    if global_weights is None or global_intercept is None:
        return model
    try:
        w = np.array(global_weights)
        b = np.array(global_intercept)
        if (w.ndim == 1 and len(w) == n_features) or \
           (w.ndim == 2 and w.shape[1] == n_features):
            model.coef_      = w
            model.intercept_ = b
            print(f"[ML RUNNER] Global weights injected for Round 2", file=sys.stderr)
        else:
            print(f"[ML RUNNER] Weight shape mismatch — training from scratch", file=sys.stderr)
    except Exception as e:
        print(f"[ML RUNNER] Weight injection failed: {e}", file=sys.stderr)
    return model

if fed_round == 2:
    model = inject_global_weights(model, global_weights, global_intercept, X_final.shape[1])

# =========================
# METRICS
# Always returns same 4 fields: accuracy, precision, recall, f1_score
# For linear_regression: r2 maps to accuracy, mse stored in precision
# This ensures frontend always shows the same fields regardless of model
# =========================
def compute_metrics(y_true, y_pred):
    if is_regression:
        mse = float(mean_squared_error(y_true, y_pred))
        r2  = float(r2_score(y_true, y_pred))
        # Convert r2 to 0-1 range (clamp negatives to 0)
        # r2=1.0 means perfect fit, r2=0.0 means baseline, negative means worse than baseline
        accuracy_equiv  = float(max(0.0, r2))           # r2 as accuracy equivalent
        precision_equiv = float(max(0.0, 1 - (mse / (np.var(y_true) + 1e-8))))  # normalised error
        precision_equiv = float(min(1.0, max(0.0, precision_equiv)))
        return {
            "accuracy":  round(accuracy_equiv, 4),
            "precision": round(precision_equiv, 4),
            "recall":    round(accuracy_equiv, 4),   # same as accuracy for regression
            "f1_score":  round(accuracy_equiv, 4),   # same as accuracy for regression
            
        }
    else:
        y_pred = np.round(y_pred).astype(int)
        return {
            "accuracy":  float(accuracy_score(y_true, y_pred)),
            "precision": float(precision_score(y_true, y_pred, average="weighted", zero_division=0)),
            "recall":    float(recall_score(y_true, y_pred, average="weighted", zero_division=0)),
            "f1_score":  float(f1_score(y_true, y_pred, average="weighted", zero_division=0))
        }

# =========================
# TRAIN + VALIDATE
# =========================
try:
    if validation_type == "kfold":
        n_splits = max(2, min(5, unique_classes if not is_regression else 5))

        if is_regression:
            cv     = KFold(n_splits=n_splits, shuffle=True, random_state=42)
            splits = list(cv.split(X_final))
        else:
            cv     = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            splits = list(cv.split(X_final, y))

        fold_metrics = []
        for train_idx, test_idx in splits:
            X_tr, X_te = X_final[train_idx], X_final[test_idx]
            y_tr = y.iloc[train_idx]
            y_te = y.iloc[test_idx]
            m = get_model()
            if fed_round == 2:
                m = inject_global_weights(m, global_weights, global_intercept, X_final.shape[1])
            m.fit(X_tr, y_tr)
            fold_metrics.append(compute_metrics(y_te, m.predict(X_te)))

        metrics = {
            key: float(np.mean([fm[key] for fm in fold_metrics]))
            for key in fold_metrics[0]
        }
        print(f"[ML RUNNER] KFold done ({len(fold_metrics)} folds)", file=sys.stderr)

    else:  # train_test_split
        stratify_y = y if (not is_regression and unique_classes >= 2) else None
        X_tr, X_te, y_tr, y_te = train_test_split(
            X_final, y, test_size=0.2, random_state=42, stratify=stratify_y
        )
        model.fit(X_tr, y_tr)
        metrics = compute_metrics(y_te, model.predict(X_te))
        print(f"[ML RUNNER] Validation: train_test_split (80/20)", file=sys.stderr)

except Exception as e:
    fail_output(f"Training failed: {e}", fed_round)

# =========================
# FINAL TRAIN ON ALL DATA
# =========================
try:
    model.fit(X_final, y)
except Exception as e:
    fail_output(f"Final training failed: {e}", fed_round)

# =========================
# EXTRACT WEIGHTS FOR FEDAVG
# =========================
local_weights   = model.coef_.tolist()
local_intercept = model.intercept_.tolist()
print(f"[ML RUNNER] Weights extracted for FedAvg", file=sys.stderr)

# =========================
# EXPLANATION (feature importances via coefficients)
# =========================
try:
    coef = np.array(model.coef_)
    if coef.ndim > 1:
        coef = np.mean(np.abs(coef), axis=0)
    col_names   = [p.name for p in numeric_parts if hasattr(p, "name")]
    explanation = {col: float(coef[i]) for i, col in enumerate(col_names) if i < len(coef)}
except Exception:
    explanation = {}

print(f"[ML RUNNER] Status: completed", file=sys.stderr)

# =========================
# OUTPUT
# =========================
print(json.dumps({
    "task_id":     task_id,
    "model":       model_type,
    "metrics":     metrics,
    "range":       f"{start_row}-{end_row}",
    "status":      "completed",
    "explanation": explanation,
    "weights":     local_weights,
    "intercept":   local_intercept,
    "round":       fed_round,
}))