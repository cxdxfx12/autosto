"""SQLite：连接、迁移、事务。

并发模型：HTTP 线程每次操作开一个短连接（WAL 模式下读写不互斥），
不做全局连接池——这个项目的写入频率是「每分钟几十次」，池只会添麻烦。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS scan_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            waybill     TEXT    NOT NULL,
            source      TEXT    NOT NULL,
            source_ref  TEXT,
            status      TEXT    NOT NULL DEFAULT 'pending',
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            result_raw  TEXT,
            screenshot  TEXT,
            batch_id    TEXT,
            created_at  TEXT    NOT NULL,
            scanned_at  TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_jobs_waybill ON scan_jobs(waybill);
        CREATE INDEX IF NOT EXISTS ix_scan_jobs_status ON scan_jobs(status, id);
        CREATE INDEX IF NOT EXISTS ix_scan_jobs_batch ON scan_jobs(batch_id, status);

        CREATE TABLE IF NOT EXISTS imports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT    NOT NULL,
            ref         TEXT    NOT NULL,
            label       TEXT,
            total       INTEGER NOT NULL DEFAULT 0,
            inserted    INTEGER NOT NULL DEFAULT 0,
            duplicated  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL,
            UNIQUE(kind, ref)
        );

        CREATE TABLE IF NOT EXISTS batches (
            id          TEXT PRIMARY KEY,
            status      TEXT NOT NULL,
            limit_n     INTEGER NOT NULL,
            note        TEXT,
            created_at  TEXT NOT NULL,
            closed_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT NOT NULL,
            actor       TEXT,
            action      TEXT NOT NULL,
            detail      TEXT
        );

        CREATE TABLE IF NOT EXISTS runtime (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  TEXT
        );
        """,
    ),
    (
        # 去重口径改成「按天」：同一个单号当天只入一次，隔天可以重新入（退回件要重揽）。
        # 原来的 ux_scan_jobs_waybill 是全局唯一，会把隔天需要重扫的单号永久挡住。
        2,
        """
        ALTER TABLE scan_jobs ADD COLUMN day TEXT;
        UPDATE scan_jobs SET day = substr(created_at, 1, 10) WHERE day IS NULL OR day = '';
        DROP INDEX IF EXISTS ux_scan_jobs_waybill;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_jobs_waybill_day ON scan_jobs(waybill, day);
        CREATE INDEX IF NOT EXISTS ix_scan_jobs_day ON scan_jobs(day, status);
        """,
    ),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0
        if row is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (0)")
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            conn.executescript(script)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 保证「取任务」不会两个手机抢到同一单。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_runtime(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM runtime WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_runtime(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO runtime(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )
