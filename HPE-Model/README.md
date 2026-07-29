# DeployIQ — Deployment Risk Scorer

A FastAPI service that scores a code change's probability of introducing a defect,
using an XGBoost model trained on the [ApacheJIT](https://arxiv.org/abs/2202.13836) dataset
and calibrated with isotonic regression. Each prediction includes a SHAP-based
explanation of which features drove the score.

See [model_card.md](model_card.md) for training details, metrics, and — importantly —
the limitations you should read before trusting a score.

## Requirements

- Python 3.12
- `fastapi`, `uvicorn`, `xgboost`, `shap`, `joblib`, `numpy`, `pydantic`, `scikit-learn`

## Setup

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install fastapi uvicorn xgboost shap joblib numpy pydantic scikit-learn
```

## Run the API

```bash
source .venv/bin/activate
uvicorn app:app --reload
```

The API starts on `http://127.0.0.1:8000`. Interactive docs (Swagger UI) are at
`http://127.0.0.1:8000/docs` and `http://127.0.0.1:8000/docs#/default/predict_risk_predict_post`.

## Usage

Send a `POST` request to `/predict` with the 12 ApacheJIT features (see
[models/feature_schema_v1.json](models/feature_schema_v1.json) for definitions):

```bash
curl -X POST http://127.0.0.1:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "la": 45,
    "ld": 12,
    "nf": 3,
    "nd": 2,
    "ns": 1,
    "ent": 1.2,
    "ndev": 4,
    "age": 30,
    "nuc": 10,
    "aexp": 50,
    "arexp": 20,
    "asexp": 15
  }'
```

The response contains a calibrated `bug_probability`, a `risk_level` band
(`LOW` / `MEDIUM` / `HIGH` / `CRITICAL`), and a `shap_explanation` with
per-feature contributions and waterfall data for visualization.

## Project files

| File | Purpose |
|---|---|
| `app.py` | FastAPI app serving the `/predict` endpoint |
| `models/xgboost_jit_v1.json` | Trained XGBoost booster |
| `models/calibrator_v1.pkl` | Isotonic probability calibrator |
| `models/feature_schema_v1.json` | Feature order, metadata, and risk thresholds |
| `models/metrics.json` | Held-out test metrics |
| `model_card.md` | Model overview, performance, and limitations |
| `DeployIQ_ApacheJIT_Training_v2 (1).ipynb` | Training notebook |
| `apachejit_total.csv` | Raw ApacheJIT dataset used for training |
