# Using enrichment-arbiter

This is the operator guide. It assumes you have cloned the repo and want to run it against real
vendors, or extend it. For the internals and the math derivations, read
[`ARCHITECTURE.md`](ARCHITECTURE.md). For a visual overview, open [`walkthrough.html`](walkthrough.html).

---

## 1. Prerequisites

- Python 3.11 or newer (the original workspace runs 3.14).
- A vendor API key for at least one provider (Hunter is the simplest free-tier option).
- `pip` and the ability to create a virtual environment.

No database server, no message queue, no cloud account is required. State is a single SQLite file.

## 2. Install

```bash
git clone https://github.com/dhrjd/enrichment-arbiter.git
cd enrichment-arbiter

python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

Run everything as `python run.py <command>`. On Windows you can also use
`.venv\Scripts\python.exe run.py <command>`.

## 3. Provide secrets

The engine reads keys from the environment first, then from a `.env` file. Only names are documented
here; never commit values.

```bash
# macOS / Linux
export HUNTER_API_KEY="..."
export PROSPEO_API_KEY="..."
export FINDYMAIL_API_KEY="..."
export APOLLO_API_KEY="..."
```

```powershell
# Windows PowerShell
$env:HUNTER_API_KEY = "..."
```

The default file fallback is `~/AppData/Local/hermes/.env` on Windows. Override it with
`HERMES_ENV_FILE=/absolute/path/to/.env`.

Supported names today: `HUNTER_API_KEY`, `PROSPEO_API_KEY`, `FINDYMAIL_API_KEY`, `APOLLO_API_KEY`.

> A missing key is not an error. The adapter returns `no_api_key` and the vendor is skipped without
> being billed, counted as a failure, or cached as a negative result.

## 4. Initialize

```bash
python run.py init
python run.py doctor
```

`init` creates `state/arbiter.db` (WAL mode) and seeds four providers with baseline costs. `doctor`
reports whether dependencies, the database, and each secret are present.

Expected `doctor` shape:

```json
{
  "ok": true,
  "db_exists": true,
  "env_file_exists": true,
  "secrets_present": { "HUNTER_API_KEY": true, "PROSPEO_API_KEY": false },
  "deps_ok": true
}
```

## 5. Manage providers

Seed defaults (edit `config.py` or override with the CLI):

```bash
python run.py provider add --id hunter --task work_email --cost-match 0.02 --max-rps 5
python run.py provider add --id prospeo --task work_email --cost-match 0.03 --max-rps 5
python run.py provider list
```

`cost_per_attempt` is the price of calling even on a miss (often `0.0` for credit-on-match vendors).
`cost_per_match` is the price of a returned record. Set them from your actual contract; the engine's
ranking is only as honest as these numbers.

To pause a vendor without deleting it, re-add it with `--inactive`.

## 6. Choose a segment

A segment is a slice of traffic with its own scoreboard. The default is `global`. Use segments when a
vendor behaves differently by market, company size, or source:

```bash
python run.py enrich --task work_email --segment us_saas ...
python run.py scorecard --segment us_saas
```

There is no automatic segmentation yet; the caller decides. Until a segment has data, its vendors share
the prior score and tie-break by id.

## 7. Enrich one lead

```bash
python run.py enrich \
  --task work_email \
  --first-name Ada --last-name Lovelace \
  --domain acme.com \
  --max-budget 0.10
```

Flags:

| Flag | Meaning |
|---|---|
| `--task` | `work_email`, `mobile_phone`, or `company_firmographics` |
| `--segment` | Scoreboard slice; default `global` |
| `--first-name`, `--last-name`, `--domain`, `--linkedin-url`, `--email` | Identity inputs |
| `--max-budget` | Hard dollar cap for this call; falls back to `DEFAULT_MAX_BUDGET` (`0.10`) |
| `--no-explore` | Disable the 5% exploration slot for deterministic runs |
| `--dry-run` | Use deterministic mock adapters; no network, no spend |
| `--request-id` | Attach your own trace id (also useful for feedback) |
| `--json` | Read the request from a file path or an inline JSON string |
| `--out-dir` | Where to write the run artifact (default `runs/`) |

### JSON input

```bash
python run.py enrich --json request.json --max-budget 0.10
```

```json
{
  "task_type": "work_email",
  "segment": "global",
  "first_name": "Ada",
  "last_name": "Lovelace",
  "domain": "acme.com",
  "max_cost_budget": 0.10,
  "request_id": "lead-1234"
}
```

`--json` accepts either a path or the raw JSON text.

### Reading the response

```json
{
  "success": true,
  "data": { "email": "â€¦@acme.com" },
  "provider_used": "hunter",
  "waterfall_trace": [
    { "provider_id": "hunter", "status": "verified", "matched": true,
      "verified": true, "latency_ms": 1840, "cost": 0.02, "explore": false }
  ],
  "total_cost": 0.02,
  "cached": false,
  "negative": false
}
```

Trace statuses:

| Status | Meaning |
|---|---|
| `cache_hit` | Served from a positive cache entry; no API call |
| `cache_negative` | Served from a negative cache entry; no API call |
| `verified` | Vendor returned data that passed verification; waterfall stopped |
| `unverified` | Vendor returned data that failed verification; waterfall continued |
| `miss` | Vendor returned no data; waterfall continued |
| `error` | Transport failure (429/5xx/timeout/exception); breaker incremented |
| `skipped_budget` | Calling this vendor would exceed `--max-budget` |
| `skipped_unconfigured` | Missing API key or missing inputs; not billed, not a failure |

`negative: true` means every attempted vendor was a clean miss and a short negative cache entry was
written.

## 8. Rehearse without spending

```bash
python run.py enrich --dry-run --task work_email \
  --first-name Ada --last-name Lovelace --domain acme.com --max-budget 0.10
```

The mock adapters are deterministic functions of the lookup hash, so a dry run is reproducible. This is
the right way to test your pipeline wiring.

## 9. Read the scorecard

```bash
python run.py scorecard --task work_email
```

`scorecard` recomputes scores first, then prints one row per `(vendor, task, segment)`:

| Column | Meaning |
|---|---|
| `total_attempts` | Calls in the 30-day window |
| `total_matches` | Calls that returned any data |
| `verified_hits` | Calls that returned data passing verification |
| `bounced` | Values later marked as hard bounces |
| `p95_latency_ms` | 95th-percentile latency over the last 100 attempts |
| `ecpvh` | Effective cost per verified hit |
| `routing_score` | Composite score used for ordering |
| `consecutive_failures`, `cooldown_until` | Circuit-breaker state |

If a vendor shows `routing_score: -999`, it has been tried and has zero effective verified hits. It is
ranked last but still reachable late in the waterfall and by exploration.

## 10. Recompute scores

Scores recompute automatically after 20 new log rows. Force it:

```bash
python run.py recalc
```

Recalculation is a read-only aggregate over `enrichment_logs`; it never calls vendors.

## 11. Feed back bounces and replies

Verification proves the domain, not the mailbox. Ground truth comes from what happens after sending.
When your outreach tool reports a hard bounce, tell the engine:

```bash
python run.py feedback --email someone@acme.com --event bounce
python run.py feedback --request-id lead-1234 --event reply
```

What happens:

1. Every log row matching that email (or request id) gets `feedback = bounce|reply`.
2. On a bounce, `verified` is set to `0`.
3. The matching cache entry is deleted, so the bad address is never served again.
4. Scores are recomputed; each bounce subtracts two from that vendor's effective verified count.

If you later expose the HTTP API, the webhook is equivalent to `POST /v1/feedback` with
`{ "email": "...", "event": "bounce" }`.

## 12. Active benchmarking (spends money)

`golden-dataset.json` contains known-truth records. A probe calls every provider for every record and
compares the returned value to the truth.

```bash
python run.py golden-probe --yes --max-budget 0.50
python run.py golden-probe --dry-run --yes --max-budget 0.50   # rehearsal
```

Both `--yes` and `--max-budget` are required so an automated script cannot accidentally drain credits.
Probes write a report under `runs/` and do not change production scores today.

Fill in real truths before relying on the report; the shipped file uses placeholders.

## 13. Caching behavior

- Cache key: `SHA-256(task_type | normalized identity)`.
- Normalization lowercases fields and strips `https://`, paths, and `www.` from domains.
- Identity priority: email, then LinkedIn URL, then name + domain (people); domain (companies).
- Positive entries live 30 days; negative entries live 7 days.
- Changing a provider's cost does not invalidate the cache; changing the returned value requires a
  bounce feedback or manual deletion.

To clear the cache manually:

```sql
DELETE FROM enrichment_cache;          -- all entries
DELETE FROM enrichment_cache WHERE lookup_hash = '<hash>';
```

Run this against `state/arbiter.db` with any SQLite client while the engine is idle.

## 14. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Trace `skipped_unconfigured` with `no_api_key` | Key not visible to the process | Export the env var or add it to the `.env` file; run `doctor` |
| Trace `skipped_unconfigured` with `missing_inputs` | Adapter needs fields you did not supply | Add the required names/domain |
| All providers `error` with `http_401`/`403` | Bad or expired key | Rotate the key |
| `http_429` repeated | Vendor rate limit | Wait; the breaker opens after 3 consecutive failures |
| `ecpvh` looks wrong | `cost_per_match` is an estimate | Update costs with `provider add` |
| `routing_score: -999` | Vendor has zero effective verified hits | It recovers as verified hits accumulate; check for bounces |
| `bank database is locked` | Concurrent writers | Run one engine process at a time; SQLite is single-writer |
| Nothing verifies | DNS/MX blocked on your network | Check DNS; the default verifier needs network access |
| `scorecard` empty | No providers seeded | Run `init` |

Use `--dry-run` to separate logic problems from vendor/network problems.

## 15. Cost control

- Always set `--max-budget` on `enrich`; the default is `0.10`.
- Set `cost_per_match` honestly; the ranking is only as good as these numbers.
- Keep negative caching on; it prevents repeated spend on unfindable records.
- Use `--no-explore` for deterministic, cost-minimal runs, and leave exploration on for learning.
- Reconcile `enrichment_logs.cost_incurred` against vendor invoices monthly and adjust costs.
- Run `golden-probe` sparingly; it is an active benchmark, not a monitoring loop.

## 16. Adding a provider

Create `providers/yourvendor.py`:

```python
from __future__ import annotations

import httpx

import config
from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult

ENDPOINT = "https://api.yourvendor.com/v1/find"
TIMEOUT = 12.0


class YourVendorProvider(BaseProvider):
    id = "yourvendor"
    task_types = (TaskType.work_email.value,)

    async def _fetch(self, req: EnrichmentRequest) -> ProviderResult:
        key = config.secret_for_provider(self.id)
        if not key:
            return ProviderResult.error("no_api_key")

        n = req.normalized()
        if not n["domain"] or not n["first_name"]:
            return ProviderResult.error("missing_inputs")

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                params={"domain": n["domain"], "first": n["first_name"],
                        "last": n["last_name"]},
            )

        if resp.status_code != 200:
            return ProviderResult(matched=False, status_code=resp.status_code,
                                  error_code=f"http_{resp.status_code}")

        payload = resp.json()
        email = payload.get("email")
        if not email:
            return ProviderResult(matched=False, status_code=200)

        return ProviderResult(matched=True, status_code=200, value=email,
                              data={"email": email})
```

Then:

1. Register it in `providers/base.py` â†’ `get_registry()`.
2. Add the seed/cost row in `config.py` (`PROVIDER_SEEDS`) or via `provider add`.
3. Add the key name to the docs and to `engine.json`'s `secrets` list if you use that runner.
4. Write a test with a scripted response; the adapter must never raise.

Rules the base class enforces: never let an exception escape `_fetch`, always set `status_code`, and
return `matched=False` with `error_code="no_api_key"` when the key is absent.

## 17. Batch use

Today the engine enriches one lead per process. For a list, wrap it:

```bash
while IFS=, read -r first last domain; do
  python run.py enrich --task work_email \
    --first-name "$first" --last-name "$last" --domain "$domain" \
    --max-budget 0.10 --no-explore
done < leads.csv
```

Batch/portfolio routing (spend a fixed budget across a list to maximize expected verified hits) is on
the roadmap. Until then, the cache and negative cache already prevent duplicate spend within and across
runs.

## 18. Security and data handling

- Keys live in the environment or a local `.env`; they are never written to the database or logs.
- `enrichment_logs.result_value` stores returned emails/phones for bounce attribution. Treat the SQLite
  file as sensitive PII and set a retention policy.
- `state/`, `runs/`, and `.venv/` are gitignored.
- The engine makes outbound calls only to the vendor endpoints you configure and to DNS for MX checks.
- There is no telemetry.

## 19. FAQ

**Do I need all vendors?** No. One key is enough; missing vendors are skipped cleanly. More vendors
improve coverage and make the waterfall meaningful.

**Will it always pick the cheapest?** No. It balances yield, cost, and latency. A cheap vendor that
rarely returns a verifiable value can rank below a pricier reliable one.

**Why did a never-used vendor rank above my working vendor?** Bayesian priors give untested vendors a
neutral baseline. With few samples, one slow success can sit below that baseline. It settles with
volume.

**Can I use it without any paid keys?** Yes. `--dry-run` uses mock adapters, and `init`, `doctor`,
`status`, `scorecard`, `recalc`, and `feedback` are free.

**Where is the data stored?** `state/arbiter.db`. Back it up by copying the file while the engine is
idle.

