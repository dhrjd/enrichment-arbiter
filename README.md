# enrichment-arbiter

**Buy the verified hit, not the call.** A self-hosted, cost-optimizing router that decides which data
vendor to ask for a work email, phone number, or company record â€” and learns from what actually works.

[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-11%20passing-0f9d76)](#testing)
[![Status](https://img.shields.io/badge/status-built%20not%20run-6b7280)](#status)
[![License](https://img.shields.io/badge/license-source--available-6b7280)](#license)

- CLI-first engine, JSON on stdout, SQLite state, no daemon.
- Dynamic waterfall: cache â†’ rank â†’ budget â†’ call â†’ verify â†’ log â†’ learn.
- Ranks vendors by **Effective Cost Per Verified Hit (ECPVH)**, not by raw match rate.
- Verifies results before accepting them (email syntax + DNS MX today; pluggable paid verifier).
- Learns from production outcomes and downstream bounces.
- Ships adapters for Hunter, Prospeo, Findymail, and Apollo, plus a deterministic mock.

> This repo is the engine. It is built and tested offline; it has not been run against live
> vendors yet. No customer data, credentials, or outcome metrics are included.
>
> Portfolio walkthrough: <https://dheerajdola.pages.dev/projects/enrichment-arbiter/>

---

## Table of contents

- [The problem](#the-problem)
- [How it works](#how-it-works)
- [The economics in one screen](#the-economics-in-one-screen)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Commands](#commands)
- [Example](#example)
- [How verification works](#how-verification-works)
- [Testing](#testing)
- [Documentation](#documentation)
- [Compared with Clay-style waterfalls](#compared-with-clay-style-waterfalls)
- [Roadmap](#roadmap)
- [Known limits](#known-limits)
- [License](#license)

---

## The problem

Sales and GTM teams need work emails and firmographics. Several vendors sell them. They disagree, cost
different amounts, perform differently per market, and change over time. The three common approaches
all waste something:

| Approach | What it costs you |
|---|---|
| Always call vendor A | Overpays; capped by one vendor's coverage |
| Call every vendor and merge | Pays everyone for every record |
| Fixed waterfall A â†’ B â†’ C | Order never adapts when a vendor improves or degrades |

`enrichment-arbiter` treats vendor selection as a learned, auditable optimization problem: **minimize
dollars per verified data point, per task and per segment**, while staying fail-safe.

## How it works

```
request
  â”‚
  â”œâ”€ cache hit? â”€â”€ yes â”€â”€â–¶ return (positive or negative), $0.00
  â”‚
  â–¼
rank active vendors for (task, segment) by routing_score
  â”‚
  â”œâ”€ 5% exploration: promote a random lower-ranked vendor to #1
  â–¼
for each vendor (cheapest-best first):
  â”œâ”€ budget gate: spent + cost_attempt + cost_match <= max_budget ?
  â”œâ”€ call adapter (timed, exceptions contained)
  â”œâ”€ transport error? â†’ circuit breaker, log, continue
  â”œâ”€ no data?        â†’ log, continue
  â”œâ”€ data returned?  â†’ verify
  â”‚     â”œâ”€ verified â†’ cache, log, STOP
  â”‚     â””â”€ not verified â†’ log, continue
  â–¼
exhausted: if every attempt was a clean miss, write a short negative cache entry
```

Every attempt is logged with cost, latency, verification, and the returned value, so scores can be
recomputed and bounces can be attributed to the vendor that produced the bad data.

## The economics in one screen

For each vendor, over a rolling 30-day window, with Bayesian priors so small samples don't lie:

```
effective_verified = max(0, verified_hits - 2 * bounced)

match_rate     = (matches + 4) / (attempts + 10)
accuracy_rate  = (effective_verified + 3) / (matches + 4)

ecpvh = (cost_per_attempt + match_rate * cost_per_match)
        / (match_rate * accuracy_rate)

score = w1 * (match_rate * accuracy_rate)
      - w2 * (ecpvh / MAX_ALLOWED_COST)
      - w3 * (p95_latency_ms / MAX_ALLOWED_LATENCY_MS)
```

A vendor that has been tried but has zero effective verified hits is floored to `-999` (ranked last,
still reachable). Everything is tunable in `config.py`. Full derivation and worked examples are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quickstart

```bash
git clone https://github.com/dhrjd/enrichment-arbiter.git
cd enrichment-arbiter

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python run.py init
python run.py doctor

# No-spend rehearsal (deterministic mock adapters)
python run.py enrich --dry-run --task work_email \
  --first-name Ada --last-name Lovelace --domain acme.com --max-budget 0.10

# Scoreboard
python run.py scorecard --task work_email
```

To make real calls, export a vendor key (see [Configuration](#configuration)) and drop `--dry-run`:

```bash
python run.py enrich --task work_email \
  --first-name Ada --last-name Lovelace --domain acme.com --max-budget 0.10
```

## Configuration

### Secrets

Keys are read from the process environment, then from a local `.env` file. Only the **names** appear in
this repo.

```bash
export HUNTER_API_KEY=...
export PROSPEO_API_KEY=...
export FINDYMAIL_API_KEY=...
export APOLLO_API_KEY=...
```

By default the loader also checks `~/AppData/Local/hermes/.env` on Windows (this project originated in a
private local workspace). Set `HERMES_ENV_FILE` to point elsewhere, or just use environment variables.

A vendor without a key is **skipped cleanly** (`no_api_key`): it is not billed, does not count as a
failure, and does not trip the circuit breaker.

### Provider costs

Providers are seeded by `init` and can be updated with `provider add`. Costs are estimates; calibrate
them to your invoices.

```bash
python run.py provider add --id hunter --task work_email --cost-match 0.02 --max-rps 5
python run.py provider list
```

## Commands

| Command | Spends money | What it does |
|---|---|---|
| `init` | no | Create the SQLite database and seed providers |
| `doctor` | no | Check dependencies, secrets, and the database |
| `status` | no | Row counts and current scores |
| `provider add` | no | Register or update a provider |
| `provider list` | no | List providers and cooldowns |
| `enrich` | **yes** | Run the waterfall for one lead |
| `scorecard` | no | Recompute, then print rankings, rates, and ECPVH |
| `recalc` | no | Recompute scores from logs now |
| `feedback` | no | Ingest a bounce/reply by email or request id |
| `golden-probe` | **yes** | Probe providers against the golden dataset (`--yes --max-budget`) |

Run `python run.py --help` or `python run.py <command> --help` for every flag.

## Example

```json
{
  "ok": true,
  "success": true,
  "data": { "email": "[redacted]" },
  "provider_used": "hunter",
  "waterfall_trace": [
    {
      "provider_id": "hunter",
      "status": "verified",
      "matched": true,
      "verified": true,
      "latency_ms": 1840,
      "cost": 0.02,
      "explore": false
    }
  ],
  "total_cost": 0.02,
  "cached": false
}
```

Immediately repeating the same lookup returns `"cached": true` and `"total_cost": 0.0` with no API call.

## How verification works

Verification is dispatched per task, because "verified" means different things:

| Task | Default check (free, local) |
|---|---|
| `work_email` | Regex syntax + DNS MX lookup (A-record fallback) |
| `mobile_phone` | E.164 normalization and digit-length check |
| `company_firmographics` | Required keys present and non-empty |

No raw SMTP probes (port 25 is blocked on most cloud hosts). A paid HTTP verifier can be plugged in
behind the same function without touching the router.

## Testing

```bash
pip install pytest pytest-asyncio
python -m pytest -q
# 11 passed
```

Tests use a temporary SQLite database and scripted adapters. They never touch the network and never
spend money. Coverage includes routing order, budget cutoff, positive/negative caching, the circuit
breaker, bounce feedback, cold-start/zero-division math, and the golden-probe gate.

## Documentation

- [`docs/USAGE.md`](docs/USAGE.md) â€” detailed operator guide: every command, flags, JSON input, cache
  behavior, feedback integration, troubleshooting, and how to add a provider.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) â€” data model, full scoring derivations, lifecycle,
  failure taxonomy, extension points, and design limits.
- [`docs/walkthrough.html`](docs/walkthrough.html) â€” a self-contained visual walkthrough (open in a
  browser; no external assets).

## Compared with Clay-style waterfalls

Platforms such as Clay provide a broad GTM workspace with configurable waterfall enrichment: you order
the providers, and the platform tries them in sequence. This engine is narrower and deeper:

| | enrichment-arbiter | Clay-style waterfall |
|---|---|---|
| Form factor | Self-hosted CLI engine / library | Hosted no-code platform |
| Order | **Learned** from live outcomes, per task and segment | Configured by the user |
| Cost awareness | ECPVH ranking, budget gates, negative cache | Credit-based, manually managed |
| Verification | Built-in gate before accepting a value | Usually a separate step |
| Bounce learning | Automatic per-vendor penalty | Generally manual |
| Data control | Keys and data stay local | Data flows through the platform |
| Time to value | Higher (needs keys + CLI) | Low |

They are complementary: use a platform for breadth and workflow, and this engine where you want strict
cost control, a learned vendor order, and bounce-driven accuracy.

## Roadmap

- HTTP API (`/v1/enrich`, `/v1/scorecard`, `/v1/feedback`) and a small dashboard.
- More live adapters (Tomba, Snov, Enrow, PeopleDataLabs; phone and firmographics tasks).
- Rate limiting from `max_rps`, retries with backoff and jitter.
- Thompson sampling / UCB instead of fixed epsilon for faster learning.
- Batch/portfolio routing: spend a fixed budget across a list to maximize expected verified hits.
- Cross-provider consensus, adaptive TTLs, spend forecasting, and vendor SLA monitoring.

## Known limits

- A single slow success can rank below untested vendors because Bayesian priors dominate small samples.
  This is deliberate conservatism; tune priors or add a minimum-sample gate if needed.
- MX proves the domain accepts mail, not that the mailbox exists. Only feedback closes that gap.
- Costs are configured estimates, not invoice truth.
- SQLite is single-writer; batch concurrency control is not built yet.
- `result_value` stores returned emails locally for attribution. Set a retention policy before production.

## Status

Built and tested offline. It has **not** been run against live vendors yet, so no outcome
metrics are claimed. The HTTP layer is not built yet.

## License

Source-available for portfolio and evaluation use. No license file is included; add one before
redistributing or using commercially.

---

Built by [Dheeraj](https://github.com/dhrjd). Product decisions, scoring policy, and safety rules are
human; much of the implementation was done with coding agents.

