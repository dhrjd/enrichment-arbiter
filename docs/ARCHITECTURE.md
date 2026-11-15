# Architecture

This document explains how `enrichment-arbiter` is built and why. For step-by-step usage, see
[`USAGE.md`](USAGE.md); for a visual version, open [`walkthrough.html`](walkthrough.html).

---

## 1. Design goals

1. **Buy verified outcomes, not calls.** Rank on cost per verified hit.
2. **Never crash the pipeline on a vendor.** Every failure is a value, not an exception.
3. **Spend is explicit and capped.** No command spends without a budget.
4. **Learn from reality, including bounces.** Scores update from logs and feedback.
5. **Per task, per segment.** One vendor can be great in one market and bad in another.
6. **Self-hosted and auditable.** Local SQLite, local keys, append-only logs.
7. **Boring operations.** A CLI and a file, not a daemon and a scheduler.

Non-goals for now: being a CRM, owning a people database, a web UI, or real-time streaming.

## 2. Two planes

| Plane | Modules | Trigger |
|---|---|---|
| Execution | `router.py`, `verifier.py`, `providers/*` | Every lookup |
| Control | `evaluator.py`, `run.py` admin commands | After N logs, on demand, or via cron |

Both run in one process, but the code boundary is clean so they can be split later.

## 3. Component map

```
run.py            CLI dispatch, JSON output, run artifacts
config.py         paths, secret loading, scoring constants, provider seeds
models.py         Pydantic v2 request/response/trace models + normalization/hash
db.py             SQLite connection (WAL), schema, cache + provider helpers
router.py         cache, ranking, exploration, budget, calls, breaker, logging
verifier.py       task-dispatched verification (syntax+MX, phone, firmographics)
evaluator.py      smoothed rates, ECPVH, score, p95, recalc, golden probe
providers/base.py ProviderResult, BaseProvider, registry
providers/*.py    vendor adapters + deterministic mock
tests/            11 deterministic tests, temp DB, no network
```

## 4. Data flow

```
EnrichmentRequest
  â†’ lookup_hash = SHA256(task | normalized identity)
  â†’ cache lookup (positive or negative, TTL-bound)
  â†’ ranked_providers(task, segment)
  â†’ optional epsilon-greedy promotion
  â†’ for each provider: budget gate â†’ adapter â†’ classify outcome
      â†’ transport error: breaker + log + continue
      â†’ unconfigured: skip (no bill, no failure)
      â†’ miss: log + continue
      â†’ match: verify â†’ log â†’ (verified ? cache + stop : continue)
  â†’ clean-miss-only exhaustion: negative cache
  â†’ lazy recalc
  â†’ EnrichmentResponse(trace, cost, cached)
```

## 5. Data model

SQLite with WAL. Four tables.

### providers

Static config and health. Primary key is `(id, task_type)` because one vendor can serve several tasks
with different pricing.

| Column | Type | Notes |
|---|---|---|
| `id`, `task_type` | TEXT | Composite PK |
| `cost_per_attempt` | REAL | Charged even on a miss |
| `cost_per_match` | REAL | Charged when data is returned |
| `max_rps` | INTEGER | Declared rate limit (not enforced yet) |
| `is_active` | INTEGER | Soft switch |
| `consecutive_failures` | INTEGER | Breaker counter |
| `cooldown_until` | TEXT | ISO timestamp; excluded while in the future |

### provider_scores

One row per `(provider, task, segment)`. A derived cache of the logs.

| Column | Notes |
|---|---|
| `total_attempts`, `total_matches`, `verified_hits`, `bounced` | Raw counts in the 30-day window |
| `p95_latency_ms` | Over the last 100 attempts |
| `ecpvh` | Effective cost per verified hit |
| `routing_score` | Composite score |
| `updated_at` | Drives lazy recalc |

### enrichment_logs

Immutable audit trail, the source of truth for learning and reconciliation.

| Column | Notes |
|---|---|
| `request_id` | Trace a logical request |
| `provider_id`, `task_type`, `segment` | Scope |
| `status_code`, `matched`, `verified` | Outcome |
| `feedback` | `NULL`, `bounce`, or `reply` |
| `cost_incurred`, `latency_ms` | Economics and speed |
| `lookup_hash` | Links to the cache key for eviction |
| `result_value` | The returned email/phone, for bounce attribution |
| `created_at` | Window and p95 ordering |

### enrichment_cache

| Column | Notes |
|---|---|
| `lookup_hash` | PK |
| `normalized_payload` | Verified data JSON, or `NULL` for negative |
| `source_provider` | Winner |
| `matched`, `verified` | Positive vs negative |
| `expires_at` | TTL boundary |

## 6. Scoring

### 6.1 Counts and rates

Over a rolling 30-day window per scope:

- `attempts`: calls made
- `matches`: calls returning any data
- `verified_hits`: calls returning data that passed verification
- `bounced`: values later reported as hard bounces

Raw rates:

```
MatchRate    = matches / attempts
AccuracyRate = verified_hits / matches
```

### 6.2 Bounce penalty

A bounce is worse than a miss: it wastes a send and damages sender reputation. Each bounce subtracts
two from the effective verified count, floored at zero.

```
effective_verified = max(0, verified_hits - 2 * bounced)
```

### 6.3 Bayesian smoothing

Small samples are noisy. Priors mean "assume 4 matches in 10 attempts, and 3 verified among those 4."

```
MatchRate    = (matches + 4) / (attempts + 10)
AccuracyRate = (effective_verified + 3) / (matches + 4)
```

The priors also guarantee the denominator is never zero.

### 6.4 ECPVH

Expected cost of one call:

```
E[cost per call] = cost_per_attempt + MatchRate * cost_per_match
```

Probability one call yields a verified hit:

```
P(verified) = MatchRate * AccuracyRate
```

Therefore:

```
ECPVH = (cost_per_attempt + MatchRate * cost_per_match)
        / (MatchRate * AccuracyRate)
```

which is algebraically:

```
ECPVH = cost_per_attempt / (MatchRate * AccuracyRate)
      + cost_per_match / AccuracyRate
```

### 6.5 Composite routing score

```
S = w1 * (MatchRate * AccuracyRate)
  - w2 * (ECPVH / MAX_ALLOWED_COST)
  - w3 * (p95_latency_ms / MAX_ALLOWED_LATENCY_MS)
```

Defaults: `w1=1.0`, `w2=1.0`, `w3=0.5`, `MAX_ALLOWED_COST=$0.50`,
`MAX_ALLOWED_LATENCY_MS=5000`. The score is a ranking utility, not a probability.

### 6.6 Cold-start floor

If `attempts > 0` and `effective_verified == 0`, set `S = -999`. This sorts the vendor last, but it
remains reachable at the bottom of the waterfall and via exploration, so it can recover.

### 6.7 Worked example

Illustrative numbers for one vendor: attempts=1, matches=1, verified=1, bounced=0, p95=2391 ms,
`cost_per_match=$0.02`.

| Step | Calculation | Result |
|---|---|---|
| effective_verified | max(0, 1 - 0) | 1 |
| MatchRate | (1+4)/(1+10) | 0.4545 |
| AccuracyRate | (1+3)/(1+4) | 0.8000 |
| yield | 0.4545 Ã— 0.8000 | 0.3636 |
| ECPVH | (0.4545 Ã— 0.02) / 0.3636 | 0.0250 |
| reward | 1.0 Ã— 0.3636 | 0.3636 |
| cost penalty | 0.0250 / 0.50 | 0.0500 |
| latency penalty | 0.5 Ã— 2391 / 5000 | 0.2391 |
| **score** | 0.3636 âˆ’ 0.0500 âˆ’ 0.2391 | **0.0745** |

An untested vendor at `$0.03` per match and the default p95 (500 ms) scores:

```
yield = 0.40 Ã— 0.75 = 0.30
ECPVH = (0.40 Ã— 0.03) / 0.30 = 0.04
S = 0.30 - 0.08 - 0.05 = 0.17
```

So one slow success can rank below an untested vendor. This is intentional conservatism; it settles as
samples accumulate.

## 7. Failure taxonomy

| Outcome | Counts as attempt? | Billed? | Breaker? | Negative cache? |
|---|---|---|---|---|
| Verified match | yes | yes | reset | no (positive) |
| Unverified match | yes | yes | reset | no |
| Clean miss (200, no data) | yes | yes | reset | yes, if all clean misses |
| Transport error (429/5xx/timeout) | yes | maybe | increment | no |
| Missing key / inputs | no | no | no | no |
| Budget skip | no | no | no | no |

Negative caching requires: at least one attempt, no transport error, no budget skip, and no match.

## 8. Circuit breaker

- On 429/5xx/timeout/exception: `consecutive_failures += 1`.
- At 3: `cooldown_until = now + 15 minutes`.
- On any 2xx: `consecutive_failures = 0`.
- Cooled-down vendors are filtered out of `ranked_providers`.

## 9. Caching

- Key: `SHA256(task_type | normalized identity)`.
- Identity priority: email â†’ LinkedIn URL â†’ name+domain (people); domain (companies).
- Positive TTL: 30 days. Negative TTL: 7 days.
- A bounce deletes the matching cache row.
- Cache is checked before ranking, so a hit costs zero.

## 10. Verification

Dispatch per task:

| Task | Check |
|---|---|
| `work_email` | Regex syntax + DNS MX (A fallback) |
| `mobile_phone` | E.164 normalization, 7â€“15 digits |
| `company_firmographics` | Non-empty required keys |

No port-25 SMTP. The verifier is an injectable function, so a paid HTTP verifier can replace the default
without touching the router.

## 11. Feedback loop

`ingest_feedback(email|request_id, event)`:

1. Find matching logs.
2. Set `feedback` and, on bounce, `verified = 0`.
3. Delete the matching cache entry using `lookup_hash`.
4. Recompute scores.

This is the only path that turns real-world delivery outcomes into ranking changes.

## 12. Segments and recalculation

Scores are per `(vendor, task, segment)`. The caller chooses the segment; `global` is the default. A new
segment starts on priors and tie-breaks by id until it has data.

Recalculation is lazy: after each log, if at least 20 logs appeared since the last `updated_at`, recompute
inline. `recalc` forces it. There is no scheduler; use cron or a task runner for periodic jobs.

## 13. Concurrency and limits

- SQLite in WAL mode; one writer at a time.
- No internal rate limiting yet; `max_rps` is declared but not enforced.
- No retries/backoff; a transport error is recorded immediately.
- Client-side latency includes our network.
- The 30-day window and last-100 p95 are fixed in `config.py`.

## 14. Extension points

- **New provider:** subclass `BaseProvider`, implement `_fetch`, register in `get_registry()`.
- **New task:** add a `TaskType`, a verifier branch, and adapters.
- **New verifier:** pass `verify_fn` (used by tests) or swap the module default.
- **New scoring:** edit `evaluator.score` and tune `config.py`.
- **HTTP API:** wrap `router.enrich`, `evaluator.scorecard`, and `router.ingest_feedback`.

## 15. Testing strategy

Deterministic, offline, and free:

- Temporary SQLite database per test.
- Scripted adapters with a fixed queue of `ProviderResult`s.
- Injected `now`, `rng`, and `verify_fn`.
- Coverage: routing order, budget cutoff, positive/negative cache, breaker trip/recovery, bounce
  feedback, cold-start/zero-division, p95 math, and the golden-probe gate.

Run with `python -m pytest -q`.

