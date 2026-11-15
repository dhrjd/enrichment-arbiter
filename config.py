"""Configuration: paths, scoring constants, and secret loading.

Secrets are read from the process environment, falling back to the shared
hermes ``.env`` file. Only *names* live in this repo (see engine.json); values
never do.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
BACKUP_DIR = STATE_DIR / "backups"
RUNS_DIR = HERE / "runs"
DB_PATH = Path(os.environ.get("ARBITER_DB") or (STATE_DIR / "arbiter.db"))
GOLDEN_DATASET = HERE / "golden_dataset.json"

HERMES_ENV = Path(
    os.environ.get("HERMES_ENV_FILE")
    or (Path.home() / "AppData" / "Local" / "hermes" / ".env")
)


def load_secret(name: str) -> str | None:
    """Return a secret from the environment, else from the shared .env file."""
    val = os.environ.get(name)
    if val:
        return val.strip()
    try:
        for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'") or None
    except FileNotFoundError:
        pass
    return None


# --- Scoring weights and normalizers -------------------------------------
W1 = 1.0                 # reward: match_rate * accuracy_rate
W2 = 1.0                 # penalty: ecpvh / MAX_ALLOWED_COST
W3 = 0.5                 # penalty: p95_latency / MAX_ALLOWED_LATENCY_MS
MAX_ALLOWED_COST = 0.50          # dollars per verified hit
MAX_ALLOWED_LATENCY_MS = 5000

# --- Bayesian priors (standardized) -------------------------------------
PRIOR_ATTEMPTS = 10
PRIOR_MATCHES = 4
PRIOR_VERIFIED = 3
BOUNCE_PENALTY_WEIGHT = 2.0

# --- Routing / policy ----------------------------------------------------
EPSILON = 0.05
RECALC_EVERY_N_LOGS = 20
POSITIVE_TTL_DAYS = 30
NEGATIVE_TTL_DAYS = 7
SCORE_WINDOW_DAYS = 30

CIRCUIT_BREAKER_THRESHOLD = 3
COOLDOWN_MINUTES = 15

P95_WINDOW = 100
COLD_START_FLOOR = -999.0
DEFAULT_MAX_BUDGET = 0.10        # dollars per enrich() call when caller omits it

# --- Provider defaults seeded by `init` ---------------------------------
# Adjust cost_per_match to your negotiated rates; attempt cost is usually 0
# for credit-on-match vendors.
PROVIDER_SEEDS = [
    {"id": "hunter", "task_type": "work_email", "cost_per_attempt": 0.0, "cost_per_match": 0.02, "max_rps": 5},
    {"id": "prospeo", "task_type": "work_email", "cost_per_attempt": 0.0, "cost_per_match": 0.03, "max_rps": 5},
    {"id": "findymail", "task_type": "work_email", "cost_per_attempt": 0.0, "cost_per_match": 0.03, "max_rps": 5},
    {"id": "apollo", "task_type": "work_email", "cost_per_attempt": 0.0, "cost_per_match": 0.05, "max_rps": 5},
]


def secret_for_provider(provider_id: str) -> str | None:
    env_name = f"{provider_id.upper()}_API_KEY"
    return load_secret(env_name)
