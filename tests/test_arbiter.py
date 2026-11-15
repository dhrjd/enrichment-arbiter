"""Deterministic tests: temp sqlite, scripted adapters, zero network, zero spend."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

import config
import db as dbmod
import evaluator
import router
import run as runner
from db import iso, now_utc
from models import EnrichmentRequest, TaskType
from providers.base import BaseProvider, ProviderResult


class ScriptedProvider(BaseProvider):
    def __init__(self, provider_id: str, results: list[ProviderResult]):
        self.id = provider_id
        self.task_types = (TaskType.work_email.value,)
        self._results = list(results)
        self.calls = 0

    async def _fetch(self, req):
        self.calls += 1
        if not self._results:
            return ProviderResult(matched=False, status_code=200)
        return self._results.pop(0)


async def _verified(*args, **kwargs) -> bool:
    return True


async def _unverified(*args, **kwargs) -> bool:
    return False


@pytest.fixture
async def conn(tmp_path):
    c = await dbmod.connect(tmp_path / "arbiter.db")
    await dbmod.init_schema(c)
    yield c
    await c.close()


async def _add(conn, pid, cost_match=0.02, attempt=0.0, task=TaskType.work_email.value):
    await dbmod.upsert_provider(
        conn, provider_id=pid, task_type=task, cost_per_attempt=attempt,
        cost_per_match=cost_match)


def _req(domain="acme.com", **kw):
    base = dict(task_type=TaskType.work_email, first_name="Ada", last_name="Lovelace",
                domain=domain)
    base.update(kw)
    return EnrichmentRequest(**base)


def _ok(value="ada.lovelace@acme.com"):
    return ProviderResult(matched=True, status_code=200, value=value,
                          data={"email": value})


def _miss():
    return ProviderResult(matched=False, status_code=200)


def _err(code=503):
    return ProviderResult(matched=False, status_code=code)


# --- routing + cache -----------------------------------------------------

async def test_waterfall_order_and_positive_cache(conn):
    await _add(conn, "a")
    await _add(conn, "b")
    reg = {"a": ScriptedProvider("a", [_ok()]), "b": ScriptedProvider("b", [_ok()])}

    resp = await router.enrich(conn, _req(), reg, verify_fn=_verified,
                               rng=random.Random(1))
    assert resp.success and resp.provider_used == "a"
    assert reg["b"].calls == 0

    again = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert again.cached and again.provider_used == "a"
    assert reg["b"].calls == 0


async def test_budget_gate_skips_and_does_not_negative_cache(conn):
    await _add(conn, "a", cost_match=0.02)
    reg = {"a": ScriptedProvider("a", [_ok()])}
    resp = await router.enrich(conn, _req(max_cost_budget=0.0), reg,
                               verify_fn=_verified)
    assert not resp.success
    assert resp.waterfall_trace[0].status == "skipped_budget"
    assert reg["a"].calls == 0
    row = await dbmod.get_cache(conn, _req().lookup_hash())
    assert row is None


async def test_negative_cache_on_clean_miss(conn):
    await _add(conn, "a")
    reg = {"a": ScriptedProvider("a", [_miss()])}
    resp = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert not resp.success and resp.negative

    again = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert again.cached and again.negative
    assert reg["a"].calls == 1


async def test_missing_key_is_skipped_without_failure_or_cache(conn):
    await _add(conn, "a")
    reg = {"a": ScriptedProvider("a", [ProviderResult.error("no_api_key")])}
    resp = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert not resp.success
    assert resp.waterfall_trace[0].status == "skipped_unconfigured"
    assert not resp.negative
    row = await dbmod.get_provider(conn, "a", TaskType.work_email.value)
    assert row["consecutive_failures"] == 0
    assert await dbmod.get_cache(conn, _req().lookup_hash()) is None


async def test_unverified_match_continues_to_next_provider(conn):
    await _add(conn, "a")
    await _add(conn, "b")
    reg = {"a": ScriptedProvider("a", [_ok("bad@acme.com")]),
           "b": ScriptedProvider("b", [_ok("good@acme.com")])}

    async def per_value(task, value, data, **kw):
        return value == "good@acme.com"

    resp = await router.enrich(conn, _req(), reg, verify_fn=per_value,
                               rng=random.Random(1))
    assert resp.provider_used == "b"
    assert reg["a"].calls == 1 and reg["b"].calls == 1


# --- circuit breaker -----------------------------------------------------

async def test_circuit_breaker_trips_after_three_and_recovers(conn):
    await _add(conn, "a")
    reg = {"a": ScriptedProvider("a", [_err(), _err(), _err(), _ok()])}

    for _ in range(3):
        await router.enrich(conn, _req(), reg, verify_fn=_verified)

    row = await dbmod.get_provider(conn, "a", TaskType.work_email.value)
    assert row["consecutive_failures"] == 3
    assert row["cooldown_until"] is not None

    during = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert not during.success and not during.waterfall_trace

    later = now_utc() + timedelta(minutes=config.COOLDOWN_MINUTES + 1)
    rows = await dbmod.ranked_providers(conn, TaskType.work_email.value, "global", later)
    assert any(r["id"] == "a" for r in rows)


async def test_success_resets_failure_counter(conn):
    await _add(conn, "a")
    reg = {"a": ScriptedProvider("a", [_err(), _ok(), _err()])}
    await router.enrich(conn, _req(), reg, verify_fn=_verified)
    row = await dbmod.get_provider(conn, "a", TaskType.work_email.value)
    assert row["consecutive_failures"] == 1
    resp = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert resp.success
    row = await dbmod.get_provider(conn, "a", TaskType.work_email.value)
    assert row["consecutive_failures"] == 0


# --- feedback ------------------------------------------------------------

async def test_bounce_evicts_cache_and_drops_score(conn):
    await _add(conn, "a")
    reg = {"a": ScriptedProvider("a", [_ok()])}
    resp = await router.enrich(conn, _req(), reg, verify_fn=_verified)
    assert resp.success

    lh = _req().lookup_hash()
    assert await dbmod.get_cache(conn, lh) is not None

    result = await router.ingest_feedback(conn, email="ada.lovelace@acme.com",
                                          event="bounce")
    assert result["matched_logs"] == 1
    assert result["cache_evicted"] == 1
    assert await dbmod.get_cache(conn, lh) is None

    cards = await evaluator.scorecard(conn, task_type=TaskType.work_email.value)
    row = next(c for c in cards if c["provider_id"] == "a")
    assert row["bounced"] == 1
    assert row["routing_score"] == config.COLD_START_FLOOR


# --- scoring math --------------------------------------------------------

def test_cold_start_and_zero_division_guard():
    untested = evaluator.score(attempts=0, matches=0, verified=0, bounced=0,
                               cost_attempt=0.0, cost_match=0.02, latency_p95=500)
    assert untested["routing_score"] > config.COLD_START_FLOOR
    assert untested["ecpvh"] < 1e6

    zero_hits = evaluator.score(attempts=5, matches=2, verified=0, bounced=0,
                                cost_attempt=0.0, cost_match=0.02, latency_p95=500)
    assert zero_hits["routing_score"] == config.COLD_START_FLOOR

    good = evaluator.score(attempts=100, matches=60, verified=50, bounced=1,
                           cost_attempt=0.0, cost_match=0.02, latency_p95=800)
    assert config.COLD_START_FLOOR < good["routing_score"] < 1.0


def test_p95_math():
    assert evaluator.p95([]) == 500
    assert evaluator.p95([100]) == 100
    vals = list(range(1, 101))
    assert evaluator.p95(vals) == 96


# --- golden probe gate ---------------------------------------------------

def test_golden_probe_requires_yes_and_budget(monkeypatch):
    assert runner.main(["golden-probe", "--max-budget", "1.0"]) == 2
    assert runner.main(["golden-probe", "--yes"]) == 2
    assert runner.main(["golden-probe"]) == 2
