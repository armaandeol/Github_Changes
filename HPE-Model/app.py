import json
import joblib
import numpy as np
import shap
import xgboost as xgb
from fastapi import FastAPI
from pydantic import BaseModel

# 1. Initialize FastAPI app
app = FastAPI(title="DeployIQ Risk Scorer API")

# 2. Load trained artifacts at startup
model = xgb.Booster()
model.load_model("models/xgboost_jit_v1.json")
calibrator = joblib.load("models/calibrator_v1.pkl")

with open("models/feature_schema_v1.json", "r") as f:
    schema = json.load(f)

# TreeExplainer is exact for tree ensembles and expensive to build; do it once
# against the same (already best-iteration-truncated) booster used for scoring,
# per PRD Module 6 / notebook §12.
explainer = shap.TreeExplainer(model)

# 3. Define the Input Data Format using Pydantic
# These are the ApacheJIT features your notebook expects
class CommitFeatures(BaseModel):
    la: float
    ld: float
    nf: float
    nd: float
    ns: float
    ent: float
    ndev: float
    age: float
    nuc: float
    aexp: float
    arexp: float
    asexp: float
    # Note: 'lt' was dropped due to Deviation #1 in your notebook!

FEATURE_ORDER = schema["feature_order"]
RISK_THRESHOLDS = schema["risk_thresholds"]

# 4. Define the API endpoint that takes inputs and gives outputs
@app.post("/predict")
def predict_risk(features: CommitFeatures):
    # Step A: Convert input JSON object into an ordered array for XGBoost,
    # using the exact column order the model was trained on (schema's feature_order),
    # not the field declaration order of CommitFeatures.
    feature_values = features.model_dump()
    input_array = np.array([[feature_values[name] for name in FEATURE_ORDER]])

    # Step B: Get raw prediction from XGBoost model
    dmatrix = xgb.DMatrix(input_array, feature_names=FEATURE_ORDER)
    raw_prob = model.predict(dmatrix)[0]

    # Step C: Calibrate probability using calibrator
    calibrated_prob = float(calibrator.transform([raw_prob])[0])

    # Step D: Determine Risk Band from the schema's risk thresholds
    risk_band = next(
        band for band, (low, high) in RISK_THRESHOLDS.items()
        if low <= calibrated_prob < high
    )

    # Step E: SHAP explanation. Values are additive in raw margin (log-odds)
    # space, not on the calibrated probability - same contract as the
    # notebook's serialize_shap (PRD Module 6).
    shap_values = np.asarray(explainer.shap_values(input_array))[0]
    base_value = float(np.asarray(explainer.expected_value).reshape(-1)[0])

    contributions = [
        {
            "feature_name": name,
            "display_name": schema["feature_metadata"][name]["display_name"],
            "raw_value": float(input_array[0][i]),
            "shap_value": float(shap_values[i]),
            "direction": "increases_risk" if shap_values[i] > 0 else "decreases_risk",
        }
        for i, name in enumerate(FEATURE_ORDER)
    ]
    contributions.sort(key=lambda c: abs(c["shap_value"]), reverse=True)
    for rank, contribution in enumerate(contributions, start=1):
        contribution["rank"] = rank

    steps = [{"label": "Base", "value": base_value, "cumulative": base_value}]
    cumulative = base_value
    for contribution in contributions:
        cumulative += contribution["shap_value"]
        steps.append({
            "label": contribution["display_name"],
            "value": contribution["shap_value"],
            "cumulative": cumulative,
        })

    # Step F: Return output response to user
    return {
        "bug_probability": round(calibrated_prob, 4),
        "risk_level": risk_band,
        "shap_explanation": {
            "base_value": base_value,
            "output_value": round(calibrated_prob, 4),
            "contributions": contributions,
            "waterfall_data": {"steps": steps, "final": round(calibrated_prob, 4)},
        },
    }