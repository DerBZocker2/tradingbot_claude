"""Regelbasierte Beispielstrategie (UNVALIDIERT). Vollständig deterministisch, long-only, ohne Hebel/Leerverkauf.

Einstieg (flat): Close > SMA_slow  UND  EMA_fast > SMA_slow  UND  RSI < rsi_entry_max
Ausstieg (long): Close < SMA_slow  ODER  EMA_fast < SMA_slow  ODER  Close <= Trailing-Stop
Trailing-Stop = höchster Wert von (Close - k*ATR) seit Einstieg, nie fallend. Er wird nur auf Schlusskursbasis
vom Bot geprüft; es ist KEINE Stop-Order beim Broker.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd

from . import indicators as ind

DISCLAIMER = "UNVALIDIERTE Beispielstrategie - keine Anlageberatung, keine Gewinnerwartung."


@dataclass(frozen=True)
class Params:
    ema_fast: int = 20
    sma_slow: int = 50
    rsi_period: int = 14
    rsi_entry_max: float = 70
    atr_period: int = 14
    atr_stop_mult: float = 3.0

    def dict(self):
        return asdict(self)


def params_from_cfg(c) -> Params:
    return Params(**c.model_dump())


def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    o = df.copy()
    o["ema_fast"] = ind.ema(o["close"], p.ema_fast)
    o["sma_slow"] = ind.sma(o["close"], p.sma_slow)
    o["rsi"] = ind.rsi(o["close"], p.rsi_period)
    o["atr"] = ind.atr(o, p.atr_period)
    o = o.join(ind.macd(o["close"]))
    o["ret"] = ind.returns(o["close"])
    o["vol20"] = ind.volatility(o["close"], 20)
    o["dd"] = ind.drawdown(o["close"])
    o["sma20"] = ind.sma(o["close"], 20)
    return o


def trailing_stop(f: pd.DataFrame, since, p: Params) -> float | None:
    """Trailing-Stop seit Einstiegs-Signalkerze `since` (nur Daten bis zur letzten Kerze)."""
    sub = f.loc[f.index >= pd.Timestamp(since)]
    cand = (sub["close"] - p.atr_stop_mult * sub["atr"]).dropna()
    return float(cand.cummax().iloc[-1]) if len(cand) else None


def decide(row: pd.Series, in_pos: bool, stop: float | None, p: Params) -> tuple[str, list[str]]:
    """Reine Funktion. Gibt (BUY|SELL|HOLD, Begründungen) zurück."""
    need = [row.get("ema_fast"), row.get("sma_slow"), row.get("rsi"), row.get("atr")]
    if any(pd.isna(v) for v in need):
        return "HOLD", ["Indikatoren noch nicht berechenbar (zu wenig Historie)"]
    c, e, s, r = row["close"], row["ema_fast"], row["sma_slow"], row["rsi"]
    if not in_pos:
        ok = [c > s, e > s, r < p.rsi_entry_max]
        txt = [f"Close {c:.2f} {'>' if c > s else '<='} SMA{p.sma_slow} {s:.2f}",
               f"EMA{p.ema_fast} {e:.2f} {'>' if e > s else '<='} SMA{p.sma_slow}",
               f"RSI{p.rsi_period} {r:.1f} {'<' if r < p.rsi_entry_max else '>='} {p.rsi_entry_max:g}"]
        return ("BUY" if all(ok) else "HOLD"), txt
    why = []
    if c < s:
        why.append(f"Close {c:.2f} < SMA{p.sma_slow} {s:.2f}")
    if e < s:
        why.append(f"EMA{p.ema_fast} {e:.2f} < SMA{p.sma_slow} {s:.2f}")
    if stop is not None and c <= stop:
        why.append(f"Close {c:.2f} <= Trailing-Stop {stop:.2f} (ATR{p.atr_period}x{p.atr_stop_mult:g})")
    if why:
        return "SELL", why
    return "HOLD", [f"Position halten: Trend intakt, Stop {stop:.2f}" if stop is not None else "Position halten: Trend intakt"]
