"""Dynamic waterfall router: cache -> ranking -> budget -> call -> verify -> log."""

from __future__ import annotations

import json
import random
import uuid

import aiosqlite

import config
import db
import evaluator
from db import iso, now_utc
from models import EnrichmentRequest, EnrichmentResponse, TraceStep
from verifier import verify as default_verify


async def _ensure_scores(conn: aiosqlite.Connection, req: EnrichmentRequest, now) -> None:
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM provider_scores WHERE task_type = ?",
        (req.task_type.value,),
    )
    have = int((await cur.fetchone())["n"])
    await cur.close()
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM providers WHERE task_type = ? AND is_active = 1",
        (req.task_type.value,),
    )
    need = int((await cur.fetchone())["n"])
    await cur.close()
    if have < need:
        await evaluator.recalculate_scores(conn, now)


async def _log(
    conn: aiosqlite.Connection,
    *,
    request_id: str,
    provider_id: str,
    req: EnrichmentRequest,
    segment: str,
    status_code: int | None,
    matched: bool,
    verified: bool,
    cost: float,
    latency_ms: int,
    lookup_hash: str,
    result_value: str | None,
    now,
) -> None:
    await conn.execute(
        """INSERT INTO enrichment_logs
           (request_id, provider_id, task_type, segment, status_code, matched,
            verified, feedback, cost_incurred, latency_ms, lookup_hash, result_value,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
        (
            request_id, provider_id, req.task_type.value, segment, status_code,
            int(matched), int(verified), cost, latency_ms, lookup_hash,
            result_value, iso(now),
        ),
    )
    await conn.commit()


def _cached_response(row) -> EnrichmentResponse:
    matched = bool(int(row["matched"]))
    if not matched:
        return EnrichmentResponse(success=False, cached=True, negative=True,
                                  total_cost=0.0,
                                  waterfall_trace=[TraceStep(
                                      provider_id=row["source_provider"] or "cache",
                                      status="cache_negative")])
    payload = json.loads(row["normalized_payload"]) if row["normalized_payload"] else None
    return EnrichmentResponse(
        success=True, data=payload, provider_used=row["source_provider"], cached=True,
        total_cost=0.0,
        waterfall_trace=[TraceStep(provider_id=row["source_provider"] or "cache",
                                   status="cache_hit", matched=True,
                                   verified=bool(int(row["verified"])))],
    )


async def enrich(
    conn: aiosqlite.Connection,
    req: EnrichmentRequest,
    registry: dict,
    *,
    now=None,
    rng: random.Random | None = None,
    max_budget: float | None = None,
    verify_fn=None,
) -> EnrichmentResponse:
    now = now or now_utc()
    rng = rng or random.Random()
    verify_fn = verify_fn or default_verify
    segment = req.normalized()["segment"]
    budget = req.max_cost_budget
    if budget is None:
        budget = config.DEFAULT_MAX_BUDGET if max_budget is None else max_budget
    request_id = req.request_id or str(uuid.uuid4())
    lookup_hash = req.lookup_hash()

    cached = await db.get_cache(conn, lookup_hash, now)
    if cached is not None:
        return _cached_response(cached)

    await _ensure_scores(conn, req, now)
    rows = await db.ranked_providers(conn, req.task_type.value, segment, now)
    providers = [dict(r) for r in rows]
    if not providers:
        return EnrichmentResponse(success=False, total_cost=0.0)

    explore_id: str | None = None
    if len(providers) > 1 and rng.random() < config.EPSILON:
        idx = rng.randrange(1, len(providers))
        promoted = providers.pop(idx)
        providers.insert(0, promoted)
        explore_id = promoted["id"]

    trace: list[TraceStep] = []
    spent = 0.0
    attempted = 0
    had_transport_error = False
    had_budget_skip = False
    had_match = False

    for p in providers:
        pid = p["id"]
        task = req.task_type.value
        cost_attempt = float(p["cost_per_attempt"])
        cost_match = float(p["cost_per_match"])

        if spent + cost_attempt + cost_match > budget:
            had_budget_skip = True
            trace.append(TraceStep(provider_id=pid, status="skipped_budget"))
            continue

        adapter = registry.get(pid)
        if adapter is None:
            trace.append(TraceStep(provider_id=pid, status="error", error_code="no_adapter"))
            continue

        res = await adapter.fetch(req)
        cost = cost_attempt + (cost_match if res.matched else 0.0)
        explore = pid == explore_id

        # A missing key or missing inputs is a configuration gap, not a vendor
        # failure: never bill it, never trip the breaker, never negative-cache.
        if res.error_code in ("no_api_key", "missing_inputs"):
            trace.append(TraceStep(provider_id=pid, status="skipped_unconfigured",
                                   error_code=res.error_code, explore=explore))
            continue

        attempted += 1
        spent += cost

        if res.is_transport_error:
            had_transport_error = True
            await db.record_failure(
                conn, pid, task,
                threshold=config.CIRCUIT_BREAKER_THRESHOLD,
                cooldown_minutes=config.COOLDOWN_MINUTES,
                now=now,
            )
            await _log(conn, request_id=request_id, provider_id=pid, req=req,
                       segment=segment, status_code=res.status_code, matched=False,
                       verified=False, cost=cost, latency_ms=res.latency_ms,
                       lookup_hash=lookup_hash, result_value=None, now=now)
            trace.append(TraceStep(
                provider_id=pid, status="error", latency_ms=res.latency_ms,
                cost=round(cost, 6), explore=explore,
                error_code=res.error_code or f"http_{res.status_code}"))
            continue

        await db.record_success(conn, pid, task)

        if not res.matched:
            await _log(conn, request_id=request_id, provider_id=pid, req=req,
                       segment=segment, status_code=res.status_code, matched=False,
                       verified=False, cost=cost, latency_ms=res.latency_ms,
                       lookup_hash=lookup_hash, result_value=None, now=now)
            trace.append(TraceStep(provider_id=pid, status="miss",
                                   latency_ms=res.latency_ms, cost=round(cost, 6),
                                   error_code=res.error_code, explore=explore))
            continue

        had_match = True
        verified = False
        if res.value is not None:
            verified = await verify_fn(task, res.value, res.data)

        await _log(conn, request_id=request_id, provider_id=pid, req=req,
                   segment=segment, status_code=res.status_code, matched=True,
                   verified=verified, cost=cost, latency_ms=res.latency_ms,
                   lookup_hash=lookup_hash, result_value=res.value, now=now)

        trace.append(TraceStep(
            provider_id=pid, status="verified" if verified else "unverified",
            matched=True, verified=verified, latency_ms=res.latency_ms,
            cost=round(cost, 6), explore=explore))

        if verified:
            await db.put_cache(
                conn, lookup_hash=lookup_hash, task_type=task,
                payload=res.data, source_provider=pid,
                matched=True, verified=True,
                ttl_days=config.POSITIVE_TTL_DAYS, now=now,
            )
            await evaluator.maybe_recalc(conn, now)
            return EnrichmentResponse(
                success=True, data=res.data, provider_used=pid,
                waterfall_trace=trace, total_cost=round(spent, 6), cached=False)

    clean_miss = (attempted > 0 and not had_transport_error
                  and not had_budget_skip and not had_match)
    if clean_miss:
        await db.put_cache(
            conn, lookup_hash=lookup_hash, task_type=req.task_type.value,
            payload=None, source_provider=None, matched=False, verified=False,
            ttl_days=config.NEGATIVE_TTL_DAYS, now=now,
        )
    await evaluator.maybe_recalc(conn, now)
    return EnrichmentResponse(
        success=False, provider_used=None, waterfall_trace=trace,
        total_cost=round(spent, 6), cached=False, negative=clean_miss)


async def ingest_feedback(conn: aiosqlite.Connection, *, email: str | None = None,
                          request_id: str | None = None, event: str = "bounce") -> dict:
    """Mark matching logs and evict cache after a downstream bounce/reply event."""
    if not email and not request_id:
        raise ValueError("feedback needs --email or --request-id")

    where, args = [], []
    if email:
        where.append("result_value = ?")
        args.append(email.strip().lower())
    if request_id:
        where.append("request_id = ?")
        args.append(request_id)
    clause = " OR ".join(where)

    cur = await conn.execute(f"SELECT * FROM enrichment_logs WHERE {clause}", args)
    rows = await cur.fetchall()
    await cur.close()

    verified_flag = 0 if event == "bounce" else 1
    await conn.execute(
        f"UPDATE enrichment_logs SET feedback = ?, verified = ? WHERE {clause}",
        [event, verified_flag, *args],
    )
    evicted = 0
    for r in rows:
        if r["lookup_hash"]:
            evicted += await db.delete_cache(conn, r["lookup_hash"])
    await conn.commit()

    if rows:
        await evaluator.recalculate_scores(conn)
    return {"event": event, "matched_logs": len(rows), "cache_evicted": evicted}
