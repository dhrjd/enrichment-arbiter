"""Scoring math, lazy/periodic recalculation, and golden-dataset probing."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import aiosqlite

import config
from db import iso, now_utc
from models import EnrichmentRequest
from verifier import verify as default_verify


def p95(values: list[int], default: int = 500) -> int:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return default
    idx = int(0.95 * len(vals))
    return vals[min(idx, len(vals) - 1)]


def score(
    *,
    attempts: int,
    matches: int,
    verified: int,
    bounced: int,
    cost_attempt: float,
    cost_match: float,
    latency_p95: int,
) -> dict:
    """Return smoothed rates, ECPVH and routing score for one provider scope."""
    effective_verified = max(0.0, verified - config.BOUNCE_PENALTY_WEIGHT * bounced)
    match_rate = (matches + config.PRIOR_MATCHES) / (attempts + config.PRIOR_ATTEMPTS)
    accuracy_rate = (effective_verified + config.PRIOR_VERIFIED) / (
        matches + config.PRIOR_MATCHES
    )
    denom = max(match_rate * accuracy_rate, 1e-6)
    ecpvh = (cost_attempt + match_rate * cost_match) / denom
    routing = (
        config.W1 * (match_rate * accuracy_rate)
        - config.W2 * (ecpvh / config.MAX_ALLOWED_COST)
        - config.W3 * (latency_p95 / config.MAX_ALLOWED_LATENCY_MS)
    )
    cold_start = attempts > 0 and effective_verified == 0
    if cold_start:
        routing = config.COLD_START_FLOOR
    return {
        "total_attempts": attempts,
        "total_matches": matches,
        "verified_hits": verified,
        "bounced": bounced,
        "effective_verified": effective_verified,
        "match_rate": round(match_rate, 6),
        "accuracy_rate": round(accuracy_rate, 6),
        "ecpvh": round(ecpvh, 6),
        "p95_latency_ms": latency_p95,
        "routing_score": round(routing, 6),
        "cold_start": cold_start,
    }


async def _aggregate(conn: aiosqlite.Connection, provider_id: str, task_type: str,
                     segment: str, window_start: str) -> tuple[int, int, int, int]:
    cur = await conn.execute(
        """SELECT COUNT(*) AS attempts,
                  COALESCE(SUM(matched), 0) AS matches,
                  COALESCE(SUM(CASE WHEN matched = 1 AND verified = 1 THEN 1 ELSE 0 END), 0) AS verified,
                  COALESCE(SUM(CASE WHEN feedback = 'bounce' THEN 1 ELSE 0 END), 0) AS bounced
           FROM enrichment_logs
           WHERE provider_id = ? AND task_type = ? AND segment = ? AND created_at >= ?""",
        (provider_id, task_type, segment, window_start),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row["attempts"]), int(row["matches"]), int(row["verified"]), int(row["bounced"])


async def _latencies(conn: aiosqlite.Connection, provider_id: str, task_type: str,
                     segment: str, window_start: str) -> list[int]:
    cur = await conn.execute(
        """SELECT latency_ms FROM enrichment_logs
           WHERE provider_id = ? AND task_type = ? AND segment = ? AND created_at >= ?
           ORDER BY created_at DESC LIMIT ?""",
        (provider_id, task_type, segment, window_start, config.P95_WINDOW),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [int(r["latency_ms"] or 0) for r in rows]


async def recalculate_scores(conn: aiosqlite.Connection, now=None) -> int:
    """Recompute provider_scores over the rolling window. Returns rows written."""
    now = now or now_utc()
    window_start = iso(now - timedelta(days=config.SCORE_WINDOW_DAYS))

    scopes: set[tuple[str, str, str]] = set()
    cur = await conn.execute(
        "SELECT DISTINCT provider_id, task_type, segment FROM enrichment_logs "
        "WHERE created_at >= ?",
        (window_start,),
    )
    for r in await cur.fetchall():
        scopes.add((r["provider_id"], r["task_type"], r["segment"]))
    await cur.close()

    segments: set[str] = {"global"}
    cur = await conn.execute("SELECT DISTINCT segment FROM provider_scores")
    for r in await cur.fetchall():
        segments.add(r["segment"])
    await cur.close()
    cur = await conn.execute("SELECT DISTINCT segment FROM enrichment_logs")
    for r in await cur.fetchall():
        segments.add(r["segment"])
    await cur.close()

    providers = await conn.execute("SELECT * FROM providers")
    provider_rows = await providers.fetchall()
    await providers.close()
    for p in provider_rows:
        for seg in segments:
            scopes.add((p["id"], p["task_type"], seg))

    written = 0
    for provider_id, task_type, segment in sorted(scopes):
        prow = await conn.execute(
            "SELECT cost_per_attempt, cost_per_match FROM providers "
            "WHERE id = ? AND task_type = ?",
            (provider_id, task_type),
        )
        pr = await prow.fetchone()
        await prow.close()
        cost_attempt = float(pr["cost_per_attempt"]) if pr else 0.0
        cost_match = float(pr["cost_per_match"]) if pr else 0.0

        attempts, matches, verified, bounced = await _aggregate(
            conn, provider_id, task_type, segment, window_start
        )
        lat = await _latencies(conn, provider_id, task_type, segment, window_start)
        m = score(
            attempts=attempts, matches=matches, verified=verified, bounced=bounced,
            cost_attempt=cost_attempt, cost_match=cost_match,
            latency_p95=p95(lat),
        )
        await conn.execute(
            """INSERT INTO provider_scores
               (provider_id, task_type, segment, total_attempts, total_matches,
                verified_hits, bounced, p95_latency_ms, ecpvh, routing_score, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider_id, task_type, segment) DO UPDATE SET
                 total_attempts=excluded.total_attempts,
                 total_matches=excluded.total_matches,
                 verified_hits=excluded.verified_hits,
                 bounced=excluded.bounced,
                 p95_latency_ms=excluded.p95_latency_ms,
                 ecpvh=excluded.ecpvh,
                 routing_score=excluded.routing_score,
                 updated_at=excluded.updated_at""",
            (provider_id, task_type, segment, m["total_attempts"], m["total_matches"],
             m["verified_hits"], m["bounced"], m["p95_latency_ms"], m["ecpvh"],
             m["routing_score"], iso(now)),
        )
        written += 1
    await conn.commit()
    return written


async def maybe_recalc(conn: aiosqlite.Connection, now=None) -> bool:
    """Run recalc inline once RECALC_EVERY_N_LOGS new logs have accumulated."""
    now = now or now_utc()
    cur = await conn.execute("SELECT MAX(updated_at) AS last FROM provider_scores")
    row = await cur.fetchone()
    await cur.close()
    last = row["last"] if row else None
    if not last:
        await recalculate_scores(conn, now)
        return True
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM enrichment_logs WHERE created_at > ?", (last,)
    )
    n = int((await cur.fetchone())["n"])
    await cur.close()
    if n >= config.RECALC_EVERY_N_LOGS:
        await recalculate_scores(conn, now)
        return True
    return False


async def scorecard(conn: aiosqlite.Connection, task_type: str | None = None,
                    segment: str | None = None) -> list[dict]:
    sql = """SELECT s.*, p.cost_per_attempt, p.cost_per_match, p.is_active,
                    p.consecutive_failures, p.cooldown_until
             FROM provider_scores s
             LEFT JOIN providers p ON p.id = s.provider_id AND p.task_type = s.task_type
             WHERE 1=1"""
    args: list = []
    if task_type:
        sql += " AND s.task_type = ?"
        args.append(task_type)
    if segment:
        sql += " AND s.segment = ?"
        args.append(segment)
    sql += " ORDER BY s.task_type, s.segment, s.routing_score DESC"
    cur = await conn.execute(sql, args)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def golden_probe(conn, registry, *, max_budget: float, now=None,
                       dataset_path=None, verify_fn=None) -> dict:
    """Probe every provider against the golden dataset. Report-only, no DB writes."""
    now = now or now_utc()
    verify_fn = verify_fn or default_verify
    path = dataset_path or config.GOLDEN_DATASET
    records = json.loads(Path(path).read_text(encoding="utf-8"))

    results: dict[str, dict] = {}
    spent = 0.0
    for rec in records:
        req = EnrichmentRequest(**{k: rec[k] for k in rec if k not in ("truth",)})
        truth = rec.get("truth")
        for provider_id, provider in registry.items():
            if req.task_type.value not in provider.task_types:
                continue
            prow = await conn.execute(
                "SELECT cost_per_attempt, cost_per_match FROM providers "
                "WHERE id = ? AND task_type = ?",
                (provider_id, req.task_type.value),
            )
            pr = await prow.fetchone()
            await prow.close()
            cost_attempt = float(pr["cost_per_attempt"]) if pr else 0.0
            cost_match = float(pr["cost_per_match"]) if pr else 0.0
            if spent + cost_attempt + cost_match > max_budget:
                continue
            res = await provider.fetch(req)
            cost = cost_attempt + (cost_match if res.matched else 0.0)
            spent += cost
            verified = False
            if res.matched:
                verified = await verify_fn(req.task_type.value, res.value, res.data)
            bucket = results.setdefault(provider_id, {
                "provider_id": provider_id, "attempts": 0, "matches": 0,
                "verified": 0, "correct": 0, "cost": 0.0, "details": [],
            })
            bucket["attempts"] += 1
            bucket["matches"] += int(res.matched)
            bucket["verified"] += int(verified)
            bucket["cost"] = round(bucket["cost"] + cost, 6)
            correct = bool(res.value) and truth is not None and str(res.value).lower() == str(truth).lower()
            bucket["correct"] += int(correct)
            bucket["details"].append({
                "record": req.lookup_input(),
                "matched": res.matched,
                "verified": verified,
                "correct": correct,
                "latency_ms": res.latency_ms,
                "error_code": res.error_code,
            })
    return {
        "generated_at": iso(now),
        "max_budget": max_budget,
        "spent": round(spent, 6),
        "records": len(records),
        "providers": sorted(results.values(), key=lambda r: r["provider_id"]),
    }
