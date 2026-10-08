from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, kind TEXT, symbol TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT, owner TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY, owner TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS prices(symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT, PRIMARY KEY(symbol,date));
CREATE TABLE IF NOT EXISTS price_meta(symbol TEXT PRIMARY KEY, fetched_at TEXT, source TEXT, report TEXT);
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, bar_date TEXT, mode TEXT, action TEXT, price REAL,
  reasons TEXT, params TEXT, data_age_bdays INTEGER, outcome TEXT, UNIQUE(symbol,bar_date,mode));
CREATE TABLE IF NOT EXISTS paper_orders(id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE, signal_id INTEGER, symbol TEXT, side TEXT, qty REAL,
  status TEXT, created_ts TEXT, signal_bar TEXT, reserved REAL, fill_bar TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS paper_trades(id INTEGER PRIMARY KEY, order_id INTEGER UNIQUE, ts TEXT, bar_date TEXT, symbol TEXT, side TEXT, qty REAL,
  price REAL, gross REAL, fee REAL, spread_slip_cost REAL, cash_delta REAL, simulated INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE, signal_id INTEGER, symbol TEXT, isin TEXT, side TEXT, qty INTEGER,
  est_price REAL, est_value REAL, status TEXT, created_ts TEXT, updated_ts TEXT, reasons TEXT, preview TEXT, exec_qty REAL, exec_price REAL, note TEXT);
CREATE TABLE IF NOT EXISTS backtests(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, params TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS news(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, title TEXT, summary TEXT, source TEXT, sentiment REAL, UNIQUE(symbol,title));
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """Atomare Transaktion (BEGIN IMMEDIATE) - auch gegen parallele Prozesse."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).lastrowid

    # --- Zustand ---
    def get_state(self, key: str, default=None):
        r = self.one("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_state(self, key: str, value) -> None:
        self.x("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (key, json.dumps(value), now()))

    # --- Protokoll ---
    def log(self, kind: str, message: str, symbol: str | None = None, level: str = "INFO", data: dict | None = None) -> None:
        self.x("INSERT INTO events(ts,level,kind,symbol,message,data) VALUES(?,?,?,?,?,?)",
               (now(), level, kind, symbol, message, json.dumps(data, default=str) if data else None))

    # --- Lease-Lock gegen parallele Läufe ---
    def acquire_lock(self, name: str, owner: str, ttl: float) -> bool:
        t = time.time()
        with self.tx() as c:
            r = c.execute("SELECT owner, expires FROM locks WHERE name=?", (name,)).fetchone()
            if r and r["expires"] > t and r["owner"] != owner:
                return False
            c.execute("INSERT INTO locks(name,owner,expires) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, expires=excluded.expires",
                      (name, owner, t + ttl))
            return True

    def release_lock(self, name: str, owner: str) -> None:
        self.x("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))
