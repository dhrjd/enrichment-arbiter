"""SQLite (aiosqlite) schema, WAL setup, and query helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    cost_per_attempt REAL NOT NULL DEFAULT 0.0,
    cost_per_match REAL NOT NULL,
    max_rps INTEGER NOT NULL DEFAULT 5,
    is_active INTEGER NOT NULL DEFAULT 1,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT NULL,
    PRIMARY KEY (id, task_type)
);

CREATE TABLE IF NOT EXISTS provider_scores (
    provider_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    segment TEXT NOT NULL DEFAULT 'global',
    total_attempts INTEGER NOT NULL DEFAULT 0,
    total_matches INTEGER NOT NULL DEFAULT 0,
    verified_hits INTEGER NOT NULL DEFAULT 0,
    bounced INTEGER NOT NULL DEFAULT 0,
    p95_latency_ms INTEGER NOT NULL DEFAULT 500,
    ecpvh REAL NOT NULL DEFAULT 0.0,
    routing_score REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider_id, task_type, segment)
);

CREATE TABLE IF NOT EXISTS enrichment_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    provider_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    segment TEXT NOT NULL,
    status_code INTEGER,
    matched INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    feedback TEXT NULL,
    cost_incurred REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    lookup_hash TEXT,
    result_value TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_logs_lookup ON enrichment_logs(lookup_hash);
CREATE INDEX IF NOT EXISTS ix_logs_result ON enrichment_logs(result_value);
CREATE INDEX IF NOT EXISTS ix_logs_scope
    ON enrichment_logs(provider_id, task_type, segment, created_at);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    lookup_hash TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    normalized_payload JSON NULL,
    source_provider TEXT NULL,
    matched INTEGER NOT NULL,
    verified INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ensure_dirs() -> None:
    for p in (config.STATE_DIR, config.BACKUP_DIR, config.RUNS_DIR, Path(config.DB_PATH).parent):
        p.mkdir(parents=True, exist_ok=True)


async def connect(db_path: str | Path | None = None) -> aiosqlite.Connection:
    ensure_dirs()
    conn = await aiosqlite.connect(str(db_path or config.DB_PATH))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    await conn.commit()
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA)
    await conn.commit()


# --- cache ---------------------------------------------------------------

async def get_cache(conn: aiosqlite.Connection, lookup_hash: str, now: datetime | None = None):
    now = now or now_utc()
    cur = await conn.execute(
        "SELECT * FROM enrichment_cache WHERE lookup_hash = ? AND expires_at > ?",
        (lookup_hash, iso(now)),
    )
    row = await cur.fetchone()
    await cur.close()
    return row


async def put_cache(
    conn: aiosqlite.Connection,
    *,
    lookup_hash: str,
    task_type: str,
    payload: dict | None,
    source_provider: str | None,
    matched: bool,
    verified: bool,
    ttl_days: int,
    now: datetime | None = None,
) -> None:
    now = now or now_utc()
    expires = now + timedelta(days=ttl_days)
    await conn.execute(
        """INSERT INTO enrichment_cache
           (lookup_hash, task_type, normalized_payload, source_provider,
            matched, verified, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(lookup_hash) DO UPDATE SET
             task_type=excluded.task_type,
             normalized_payload=excluded.normalized_payload,
             source_provider=excluded.source_provider,
             matched=excluded.matched,
             verified=excluded.verified,
             created_at=excluded.created_at,
             expires_at=excluded.expires_at""",
        (
            lookup_hash,
            task_type,
            json.dumps(payload) if payload is not None else None,
            source_provider,
            int(matched),
            int(verified),
            iso(now),
            iso(expires),
        ),
    )
    await conn.commit()


async def delete_cache(conn: aiosqlite.Connection, lookup_hash: str) -> int:
    cur = await conn.execute("DELETE FROM enrichment_cache WHERE lookup_hash = ?", (lookup_hash,))
    await conn.commit()
    n = cur.rowcount
    await cur.close()
    return n


# --- providers -----------------------------------------------------------

async def upsert_provider(
    conn: aiosqlite.Connection,
    *,
    provider_id: str,
    task_type: str,
    cost_per_attempt: float = 0.0,
    cost_per_match: float = 0.02,
    max_rps: int = 5,
    is_active: bool = True,
) -> None:
    await conn.execute(
        """INSERT INTO providers
           (id, task_type, cost_per_attempt, cost_per_match, max_rps, is_active)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id, task_type) DO UPDATE SET
             cost_per_attempt=excluded.cost_per_attempt,
             cost_per_match=excluded.cost_per_match,
             max_rps=excluded.max_rps,
             is_active=excluded.is_active""",
        (provider_id, task_type, cost_per_attempt, cost_per_match, max_rps, int(is_active)),
    )
    await conn.commit()


async def get_provider(conn: aiosqlite.Connection, provider_id: str, task_type: str):
    cur = await conn.execute(
        "SELECT * FROM providers WHERE id = ? AND task_type = ?", (provider_id, task_type)
    )
    row = await cur.fetchone()
    await cur.close()
    return row


async def list_providers(conn: aiosqlite.Connection, task_type: str | None = None):
    if task_type:
        cur = await conn.execute(
            "SELECT * FROM providers WHERE task_type = ? ORDER BY id", (task_type,)
        )
    else:
        cur = await conn.execute("SELECT * FROM providers ORDER BY task_type, id")
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def ranked_providers(
    conn: aiosqlite.Connection, task_type: str, segment: str, now: datetime
):
    """Active, non-cooling providers ordered by routing_score DESC."""
    cur = await conn.execute(
        """SELECT p.*, COALESCE(s.routing_score, 0.0) AS routing_score
           FROM providers p
           LEFT JOIN provider_scores s
             ON s.provider_id = p.id
            AND s.task_type = p.task_type
            AND s.segment = ?
           WHERE p.task_type = ?
             AND p.is_active = 1
             AND (p.cooldown_until IS NULL OR p.cooldown_until <= ?)
           ORDER BY routing_score DESC, p.id ASC""",
        (segment, task_type, iso(now)),
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def record_failure(
    conn: aiosqlite.Connection,
    provider_id: str,
    task_type: str,
    *,
    threshold: int,
    cooldown_minutes: int,
    now: datetime,
) -> int:
    """Increment consecutive_failures; trip cooldown at threshold. Returns the count."""
    row = await get_provider(conn, provider_id, task_type)
    if row is None:
        return 0
    count = int(row["consecutive_failures"]) + 1
    cooldown = None
    if count >= threshold:
        cooldown = iso(now + timedelta(minutes=cooldown_minutes))
    await conn.execute(
        "UPDATE providers SET consecutive_failures = ?, cooldown_until = ? "
        "WHERE id = ? AND task_type = ?",
        (count, cooldown, provider_id, task_type),
    )
    await conn.commit()
    return count


async def record_success(conn: aiosqlite.Connection, provider_id: str, task_type: str) -> None:
    await conn.execute(
        "UPDATE providers SET consecutive_failures = 0 WHERE id = ? AND task_type = ?",
        (provider_id, task_type),
    )
    await conn.commit()
