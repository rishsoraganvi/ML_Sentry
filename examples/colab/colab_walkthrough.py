# %% [markdown]
# # Govern a Colab regression model with ML Sentry
#
# This notebook trains a regression model, reports its evaluation to **your ML Sentry
# server**, and acts as the **external training runner**: when a human approves
# retraining in the ML Sentry UI, this notebook claims the approved job, retrains,
# evaluates the candidate on the champion's fixed holdout and reports it back.
#
# Project: https://github.com/rishsoraganvi/ML_Sentry
#
# **Before you start** (details:
# [docs/colab.md](https://github.com/rishsoraganvi/ML_Sentry/blob/main/docs/colab.md)):
# 1. ML Sentry runs on your machine (`docker compose up -d`) with a policy for
#    `colab_regressor` (`configs/my-model.example.yaml`) and
#    `ML_SENTRY_RETRAINING_ENABLED=true` (needed for the external runner).
# 2. Colab can reach it over HTTPS, e.g. `cloudflared tunnel --url http://127.0.0.1:8000`.
# 3. In Colab's 🔑 *Secrets* panel add `ML_SENTRY_URL`, `ML_SENTRY_TOKEN` (service
#    token) and `ML_SENTRY_RUNNER_TOKEN`. **Never put a reviewer token in Colab.**

# %%
# Configuration. In Colab the values come from the Secrets panel; elsewhere from env vars.
import os
import time
import uuid

try:
    from google.colab import userdata  # type: ignore[import-not-found]

    def secret(name: str) -> str:
        return str(userdata.get(name))
except ImportError:  # running outside Colab (e.g. the local test of this notebook)

    def secret(name: str) -> str:
        return os.environ[name]


from ml_sentry_client import MLSentryClient, regression_metrics

sentry = MLSentryClient(
    secret("ML_SENTRY_URL"),
    secret("ML_SENTRY_TOKEN"),
    runner_token=secret("ML_SENTRY_RUNNER_TOKEN"),
)
MODEL_NAME = os.environ.get("ML_SENTRY_MODEL", "colab_regressor")
RUN = os.environ.get("ML_SENTRY_RUN_ID", uuid.uuid4().hex[:6])  # keeps re-runs distinct
SEED = 42

# %% [markdown]
# ## 1. Data and a fixed, versioned holdout
# Candidates are only ever compared with the champion on **this exact holdout**, so
# its identity (dataset id + version + protocol + target) is part of every report.

# %%
from sklearn.datasets import fetch_california_housing, make_regression
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import train_test_split

if os.environ.get("ML_SENTRY_SYNTHETIC_DATA"):  # offline fallback used by the local test
    X, y = make_regression(n_samples=4000, n_features=8, noise=20.0, random_state=SEED)
    y = (y - y.min()) / 100.0
    DATASET_ID = "synthetic-regression"
else:
    X, y = fetch_california_housing(return_X_y=True)
    DATASET_ID = "california-housing"

X_train, X_hold, y_train, y_hold = train_test_split(X, y, test_size=0.2, random_state=SEED)
HOLDOUT = {
    "dataset_id": DATASET_ID,
    "dataset_version": f"v1-seed{SEED}",
    "evaluation_protocol_id": "holdout-20pct-v1",
    "target_name": "median_house_value",
}
print(f"train={len(y_train)} holdout={len(y_hold)}")


def evaluate(model) -> dict[str, float]:
    return regression_metrics(list(y_hold), list(model.predict(X_hold)))


# %% [markdown]
# ## 2. Train the champion and register its baseline evaluation

# %%
champion = HistGradientBoostingRegressor(max_iter=100, random_state=SEED).fit(X_train, y_train)
champion_metrics = evaluate(champion)
print("champion", {k: round(v, 4) for k, v in champion_metrics.items()})

decision = sentry.submit_evaluation(
    event_id=f"{MODEL_NAME}-{RUN}-baseline",
    role="baseline",
    model_name=MODEL_NAME,
    model_version=f"1-{RUN}",
    sample_count=len(y_hold),
    metrics=champion_metrics,
    **HOLDOUT,
)
print("baseline:", decision["severity"], decision["proposed_action"])

# %% [markdown]
# ## 3. Report a monitoring evaluation
# In production you would evaluate the deployed model on new labelled data here.
# To demonstrate a regression we evaluate a deliberately weak model version.

# %%
degraded = HistGradientBoostingRegressor(max_iter=5, random_state=SEED).fit(X_train, y_train)
monitoring = sentry.submit_evaluation(
    event_id=f"{MODEL_NAME}-{RUN}-monitoring",
    model_name=MODEL_NAME,
    model_version=f"2-{RUN}",
    sample_count=len(y_hold),
    metrics=evaluate(degraded),
    **HOLDOUT,
)
print(monitoring["severity"], "->", monitoring["proposed_action"])
print(monitoring["decision_reason"])
INCIDENT_ID = monitoring["incident_id"]

# %% [markdown]
# ## 4. A human approves retraining in the ML Sentry UI
# Open `<your ML Sentry URL>/ui/` on your machine, enter **your reviewer token**,
# review the retraining request (evidence, pipeline, holdout) and approve it.
# Nothing in this notebook can approve anything.
#
# ## 5. Run the approved job here (external runner)
# The next cell waits for the approved job, trains with the **approved** config and
# reports the candidate's metrics on the same holdout.

# %%


def train_and_evaluate(spec: dict) -> dict:
    assert {k: spec["holdout"][k] for k in HOLDOUT} == HOLDOUT, "job is for another holdout"
    config = spec["config"]  # approved, allow-listed configuration from the policy
    model = HistGradientBoostingRegressor(
        max_iter=int(config.get("max_iter", 300)),
        learning_rate=float(config.get("learning_rate", 0.1)),
        random_state=SEED,
    ).fit(X_train, y_train)
    version = f"3-{RUN}"
    # Save the model wherever you keep artifacts (e.g. Google Drive) and reference it here.
    return {
        "candidate_model_ref": f"colab://{MODEL_NAME}/{version}",
        "candidate_model_version": version,
        "metrics": evaluate(model),
        "sample_count": len(y_hold),
    }


results = sentry.run_jobs(
    train_and_evaluate,
    model_name=MODEL_NAME,
    wait_seconds=float(os.environ.get("ML_SENTRY_WAIT_SECONDS", 600)),
    poll_seconds=5,
)
print([r["status"] for r in results])

# %% [markdown]
# ## 6. A human approves the deployment
# If the candidate passed every gate (same holdout, no regression vs the champion,
# primary metric improved), ML Sentry asks for a **second, separate** approval in
# the UI. After that it releases to the configured target and verifies it.

# %%
for _ in range(int(os.environ.get("ML_SENTRY_STATUS_POLLS", 1))):
    status = sentry.incident(INCIDENT_ID)
    print(status["status"], "/", status["workflow_status"])
    if status["status"] == "resolved":
        break
    time.sleep(5)
