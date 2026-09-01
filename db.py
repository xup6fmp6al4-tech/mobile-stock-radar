from __future__ import annotations
import os, sqlite3
from pathlib import Path
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("BLACKBOX_DB", "./data/radar_blackbox.sqlite3")
USING_POSTGRES = bool(DATABASE_URL)

SCHEMA_SQLITE = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, ts_utc INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  source TEXT NOT NULL, captured_at TEXT NOT NULL,
  PRIMARY KEY(symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(ts_utc);
CREATE TABLE IF NOT EXISTS bars_5m (
  symbol TEXT NOT NULL, ts_utc INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  source TEXT NOT NULL, archived_at TEXT NOT NULL,
  PRIMARY KEY(symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_bars5_ts ON bars_5m(ts_utc);
CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, symbol TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  ok INTEGER NOT NULL DEFAULT 0, http_status INTEGER,
  rows_written INTEGER NOT NULL DEFAULT 0,
  error TEXT, raw_meta TEXT
);
CREATE TABLE IF NOT EXISTS source_status (
  key TEXT PRIMARY KEY, label TEXT NOT NULL, state TEXT NOT NULL,
  detail TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL
);
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, ts_utc BIGINT NOT NULL,
  open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
  close DOUBLE PRECISION, volume DOUBLE PRECISION,
  source TEXT NOT NULL, captured_at TEXT NOT NULL,
  PRIMARY KEY(symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(ts_utc);
CREATE TABLE IF NOT EXISTS bars_5m (
  symbol TEXT NOT NULL, ts_utc BIGINT NOT NULL,
  open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
  close DOUBLE PRECISION, volume DOUBLE PRECISION,
  source TEXT NOT NULL, archived_at TEXT NOT NULL,
  PRIMARY KEY(symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_bars5_ts ON bars_5m(ts_utc);
CREATE TABLE IF NOT EXISTS captures (
  id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL, symbol TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  ok INTEGER NOT NULL DEFAULT 0, http_status INTEGER,
  rows_written INTEGER NOT NULL DEFAULT 0,
  error TEXT, raw_meta TEXT
);
CREATE TABLE IF NOT EXISTS source_status (
  key TEXT PRIMARY KEY, label TEXT NOT NULL, state TEXT NOT NULL,
  detail TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL
);
"""

@contextmanager
def db():
    if USING_POSTGRES:
        import psycopg
        con = psycopg.connect(DATABASE_URL)
        try:
            yield con
            con.commit()
        finally:
            con.close()
    else:
        Path(SQLITE_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(SQLITE_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

def init_db():
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute(SCHEMA_PG)
        else:
            con.executescript(SCHEMA_SQLITE)

def fetchall_dict(con, sql, params=()):
    if USING_POSTGRES:
        from psycopg.rows import dict_row
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    return [dict(r) for r in con.execute(sql, params).fetchall()]

def fetchone_dict(con, sql, params=()):
    rows = fetchall_dict(con, sql, params)
    return rows[0] if rows else None
