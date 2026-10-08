"""Kursdaten: austauschbare Provider, Persistenz, Qualitäts-/Aktualitätsprüfung."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .config import Config
from .db import DB, now

COLS = ["open", "high", "low", "close", "volume"]


def bdays_between(last: date, today: date) -> int:
    if last >= today:
        return 0
    return int(np.busday_count(last, today))


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[COLS]


class YFinanceProvider:
    name = "yfinance"

    def fetch(self, symbol, years):
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=f"{years}y", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance lieferte keine Daten für {symbol}")
        return _norm(df)


class CsvProvider:
    name = "csv"

    def __init__(self, folder):
        self.folder = folder

    def fetch(self, symbol, years):
        p = self.folder / f"{symbol}.csv"
        if not p.exists():
            raise RuntimeError(f"CSV fehlt: {p}")
        df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
        return _norm(df)


class SyntheticProvider:
    """Deterministische Zufallskurse. NUR Demo/Test - keinerlei Marktbezug."""
    name = "synthetic"

    def fetch(self, symbol, years):
        seed = sum(ord(c) for c in symbol)
        rng = np.random.default_rng(seed)
        end = pd.Timestamp(date.today())
        idx = pd.bdate_range(end=end, periods=int(252 * years))
        n = len(idx)
        regime = np.sin(np.linspace(0, 9 * np.pi, n)) * 0.0006
        r = rng.normal(0.0003 + regime, 0.011, n)
        close = 100 * np.exp(np.cumsum(r))
        gap = rng.normal(0, 0.003, n)
        open_ = np.r_[close[0], close[:-1]] * (1 + gap)
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
        return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                             "volume": rng.integers(1e6, 5e6, n).astype(float)}, index=idx)


def make_provider(cfg: Config):
    p = cfg.data.provider
    if p == "yfinance":
        return YFinanceProvider()
    if p == "csv":
        return CsvProvider(cfg.path(cfg.data.csv_dir))
    if p == "synthetic":
        return SyntheticProvider()
    raise ValueError(f"Unbekannter Datenprovider: {p}")


def quality_report(df: pd.DataFrame, source: str, today: date | None = None, max_gap_days: int = 5) -> dict:
    """Dokumentiert Datenlücken und Auffälligkeiten (keine stille Korrektur)."""
    today = today or date.today()
    rep: dict = {"source": source, "bars": int(len(df))}
    if df.empty:
        return {**rep, "last_bar": None, "age_bdays": None, "gaps": [], "invalid_rows": 0}
    d = df.index.to_series().diff().dt.days.dropna()
    gaps = [{"from": str(df.index[i - 1].date()), "to": str(df.index[i].date()), "days": int(v)}
            for i, v in zip(range(1, len(df)), d) if v > max_gap_days]
    bad = int((df[["open", "high", "low", "close"]].isna().any(axis=1) | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum())
    rep.update(first_bar=str(df.index[0].date()), last_bar=str(df.index[-1].date()),
               age_bdays=bdays_between(df.index[-1].date(), today), gaps=gaps, invalid_rows=bad,
               note="Lücke = mehr als %d Kalendertage zwischen zwei Kerzen; Feiertage nicht modelliert." % max_gap_days)
    return rep


class DataService:
    def __init__(self, cfg: Config, db: DB, provider=None):
        self.cfg, self.db = cfg, db
        self.provider = provider or make_provider(cfg)

    def update(self, symbol: str) -> dict:
        try:
            df = self.provider.fetch(symbol, self.cfg.data.history_years)
            df = df.dropna(subset=["open", "high", "low", "close"])
            rows = [(symbol, str(i.date()), r.open, r.high, r.low, r.close, r.volume, self.provider.name) for i, r in df.iterrows()]
            with self.db.tx() as c:
                c.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", rows)
            err = None
        except Exception as e:  # Netzwerk/Provider: Cache nutzen, Alter wird separat geprüft
            err = f"{type(e).__name__}: {e}"
            self.db.log("data_error", err, symbol, "WARN")
        df = self.load(symbol)
        rep = quality_report(df, self.provider.name)
        rep["fetch_error"] = err
        self.db.x("INSERT OR REPLACE INTO price_meta VALUES(?,?,?,?)", (symbol, now(), self.provider.name, json.dumps(rep)))
        return rep

    def load(self, symbol: str) -> pd.DataFrame:
        rows = self.db.q("SELECT date,open,high,low,close,volume FROM prices WHERE symbol=? ORDER BY date", (symbol,))
        if not rows:
            return pd.DataFrame(columns=COLS)
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df.pop("date"))
        return df.astype(float)

    def report(self, symbol: str) -> dict | None:
        r = self.db.one("SELECT report, fetched_at FROM price_meta WHERE symbol=?", (symbol,))
        if not r:
            return None
        rep = json.loads(r["report"])
        df = self.load(symbol)
        if not df.empty:  # Alter immer frisch berechnen
            rep["age_bdays"] = bdays_between(df.index[-1].date(), date.today())
        rep["fetched_at"] = r["fetched_at"]
        return rep

    def is_fresh(self, df: pd.DataFrame, today: date | None = None) -> tuple[bool, str]:
        if df.empty:
            return False, "keine Kurse vorhanden"
        age = bdays_between(df.index[-1].date(), today or date.today())
        if age > self.cfg.data.max_staleness_bdays:
            return False, f"Kurse veraltet: letzte Kerze {df.index[-1].date()} ({age} Handelstage alt, Limit {self.cfg.data.max_staleness_bdays})"
        if df.iloc[-1][["open", "high", "low", "close"]].isna().any():
            return False, "letzte Kerze unvollständig"
        return True, "ok"
