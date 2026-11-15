#!/usr/bin/env python
"""enrichment-arbiter — CLI entrypoint.

Contract (see ../../README.md, "The engine contract"):
  * stdout is one JSON object on success; errors go to stderr with non-zero exit
  * inputs are read-only; anything written goes under runs/ and state/
  * the network is only touched by `enrich` and `golden-probe`

Commands: init | doctor | status | provider add|list | enrich | scorecard |
          recalc | feedback | golden-probe | serve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
import db as dbmod  # noqa: E402
import evaluator  # noqa: E402
import router  # noqa: E402
from models import EnrichmentRequest, TaskType  # noqa: E402


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_run(name: str, payload, out_dir: str | None) -> Path:
    out = Path(out_dir) if out_dir else config.RUNS_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}-{_stamp()}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


class _NoExplore(random.Random):
    def random(self) -> float:  # always decline the epsilon-greedy explore slot
        return 1.0


async def _passthrough_verify(*args, **kwargs) -> bool:
    return True


async def _async_registry_for(conn, dry_run: bool) -> dict:
    if dry_run:
        from providers.mock import MockProvider
        rows = await dbmod.list_providers(conn)
        return {r["id"]: MockProvider(r["id"]) for r in rows}
    from providers.base import get_registry
    return get_registry()


# --- commands ------------------------------------------------------------

async def cmd_init(args) -> int:
    conn = await dbmod.connect()
    try:
        await dbmod.init_schema(conn)
        seeded = 0
        if not args.no_seed:
            for s in config.PROVIDER_SEEDS:
                await dbmod.upsert_provider(conn, provider_id=s["id"],
                                            task_type=s["task_type"],
                                            cost_per_attempt=s.get("cost_per_attempt", 0.0),
                                            cost_per_match=s["cost_per_match"],
                                            max_rps=s.get("max_rps", 5))
                seeded += 1
        written = await evaluator.recalculate_scores(conn)
    finally:
        await conn.close()
    _emit({"ok": True, "db": str(config.DB_PATH), "seeded": seeded, "score_rows": written})
    return 0


async def cmd_doctor(args) -> int:
    checks = {
        "db_exists": Path(config.DB_PATH).exists(),
        "env_file_exists": config.HERMES_ENV.exists(),
        "state_dir": config.STATE_DIR.exists(),
    }
    checks["secrets_present"] = {name: bool(config.load_secret(name)) for name in
                                 [f"{p['id'].upper()}_API_KEY" for p in config.PROVIDER_SEEDS]}
    try:
        import aiosqlite, dns, httpx, pydantic  # noqa: F401
        checks["deps_ok"] = True
    except Exception as exc:  # noqa: BLE001
        checks["deps_ok"] = f"missing: {exc}"
    _emit({"ok": bool(checks["db_exists"] and checks["deps_ok"]), **checks})
    return 0 if checks["db_exists"] and checks["deps_ok"] else 1


async def cmd_status(args) -> int:
    conn = await dbmod.connect()
    try:
        counts = {}
        for table in ("providers", "provider_scores", "enrichment_logs", "enrichment_cache"):
            cur = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int((await cur.fetchone())["n"])
            await cur.close()
        cur = await conn.execute(
            "SELECT s.provider_id, s.task_type, s.segment, s.routing_score, "
            "p.consecutive_failures, p.cooldown_until "
            "FROM provider_scores s LEFT JOIN providers p "
            "ON p.id = s.provider_id AND p.task_type = s.task_type "
            "ORDER BY s.task_type, s.routing_score DESC")
        scores = [dict(r) for r in await cur.fetchall()]
        await cur.close()
    finally:
        await conn.close()
    _emit({"ok": True, "db": str(config.DB_PATH), "counts": counts, "scores": scores})
    return 0


async def cmd_provider_add(args) -> int:
    conn = await dbmod.connect()
    try:
        await dbmod.init_schema(conn)
        await dbmod.upsert_provider(
            conn, provider_id=args.id, task_type=args.task,
            cost_per_attempt=args.cost_attempt, cost_per_match=args.cost_match,
            max_rps=args.max_rps, is_active=not args.inactive)
        await evaluator.recalculate_scores(conn)
    finally:
        await conn.close()
    _emit({"ok": True, "provider": args.id, "task_type": args.task})
    return 0


async def cmd_provider_list(args) -> int:
    conn = await dbmod.connect()
    try:
        rows = await dbmod.list_providers(conn, args.task)
    finally:
        await conn.close()
    _emit({"ok": True, "providers": [dict(r) for r in rows]})
    return 0


async def cmd_enrich(args) -> int:
    if args.json:
        p = Path(args.json)
        raw = p.read_text(encoding="utf-8") if p.exists() else args.json
        req = EnrichmentRequest(**json.loads(raw))
    else:
        if not args.task:
            print("enrich needs --task (or --json)", file=sys.stderr)
            return 2
        req = EnrichmentRequest(
            task_type=args.task, segment=args.segment, first_name=args.first_name,
            last_name=args.last_name, domain=args.domain,
            linkedin_url=args.linkedin_url, email=args.email,
            max_cost_budget=args.max_budget, request_id=args.request_id)

    conn = await dbmod.connect()
    try:
        await dbmod.init_schema(conn)
        registry = await _async_registry_for(conn, args.dry_run)
        rng = _NoExplore() if args.no_explore else random.Random()
        verify_fn = _passthrough_verify if args.dry_run else None
        resp = await router.enrich(conn, req, registry, rng=rng,
                                   max_budget=args.max_budget, verify_fn=verify_fn)
    finally:
        await conn.close()

    payload = resp.model_dump()
    if args.out_dir is not False:
        payload["_run_file"] = str(_write_run("enrich", payload, args.out_dir))
    _emit({"ok": True, "dry_run": args.dry_run, **payload})
    return 0


async def cmd_scorecard(args) -> int:
    conn = await dbmod.connect()
    try:
        await evaluator.recalculate_scores(conn)
        rows = await evaluator.scorecard(conn, args.task, args.segment)
    finally:
        await conn.close()
    payload = {"ok": True, "scorecard": rows}
    payload["_run_file"] = str(_write_run("scorecard", payload, args.out_dir))
    _emit(payload)
    return 0


async def cmd_recalc(args) -> int:
    conn = await dbmod.connect()
    try:
        written = await evaluator.recalculate_scores(conn)
    finally:
        await conn.close()
    _emit({"ok": True, "score_rows_written": written})
    return 0


async def cmd_feedback(args) -> int:
    if not args.email and not args.request_id:
        print("feedback needs --email or --request-id", file=sys.stderr)
        return 2
    conn = await dbmod.connect()
    try:
        result = await router.ingest_feedback(
            conn, email=args.email, request_id=args.request_id, event=args.event)
    finally:
        await conn.close()
    _emit({"ok": True, **result})
    return 0


async def cmd_golden_probe(args) -> int:
    if not args.yes:
        print("golden-probe spends vendor credits; re-run with --yes", file=sys.stderr)
        return 2
    if args.max_budget is None:
        print("golden-probe requires --max-budget", file=sys.stderr)
        return 2
    conn = await dbmod.connect()
    try:
        await dbmod.init_schema(conn)
        registry = await _async_registry_for(conn, args.dry_run)
        verify_fn = _passthrough_verify if args.dry_run else None
        report = await evaluator.golden_probe(
            conn, registry, max_budget=args.max_budget,
            dataset_path=args.dataset, verify_fn=verify_fn)
    finally:
        await conn.close()
    report["ok"] = True
    report["_run_file"] = str(_write_run("golden-probe", report, args.out_dir))
    _emit(report)
    return 0


async def cmd_serve(args) -> int:
    print("serve is not implemented yet (phase 3); use the CLI commands.",
          file=sys.stderr)
    return 2


# --- parser --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run.py", description="enrichment-arbiter")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create db + seed providers")
    p.add_argument("--no-seed", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check deps, secrets, db")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="counts and current scores")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("provider", help="manage provider config")
    psub = p.add_subparsers(dest="provider_cmd", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("--id", required=True)
    pa.add_argument("--task", required=True)
    pa.add_argument("--cost-attempt", type=float, default=0.0)
    pa.add_argument("--cost-match", type=float, required=True)
    pa.add_argument("--max-rps", type=int, default=5)
    pa.add_argument("--inactive", action="store_true")
    pa.set_defaults(func=cmd_provider_add)
    pl = psub.add_parser("list")
    pl.add_argument("--task", default=None)
    pl.set_defaults(func=cmd_provider_list)

    p = sub.add_parser("enrich", help="run the waterfall for one lead")
    p.add_argument("--task", choices=[t.value for t in TaskType])
    p.add_argument("--segment", default="global")
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--domain")
    p.add_argument("--linkedin-url")
    p.add_argument("--email")
    p.add_argument("--request-id")
    p.add_argument("--max-budget", type=float, default=None)
    p.add_argument("--json", help="path to (or inline) JSON request")
    p.add_argument("--no-explore", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="mock adapters, no spend")
    p.add_argument("--out-dir", default=None)
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("scorecard", help="rankings, rates, ECPVH")
    p.add_argument("--task", default=None)
    p.add_argument("--segment", default=None)
    p.add_argument("--out-dir", default=None)
    p.set_defaults(func=cmd_scorecard)

    p = sub.add_parser("recalc", help="recompute scores now")
    p.set_defaults(func=cmd_recalc)

    p = sub.add_parser("feedback", help="ingest bounce/reply events")
    p.add_argument("--email", default=None)
    p.add_argument("--request-id", default=None)
    p.add_argument("--event", choices=["bounce", "reply"], default="bounce")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("golden-probe", help="probe providers against golden dataset")
    p.add_argument("--max-budget", type=float, default=None)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dataset", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out-dir", default=None)
    p.set_defaults(func=cmd_golden_probe)

    p = sub.add_parser("serve", help="(phase 3) HTTP API")
    p.set_defaults(func=cmd_serve)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "out_dir", None) is None and args.command in ("enrich", "scorecard", "golden-probe"):
        args.out_dir = None
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
