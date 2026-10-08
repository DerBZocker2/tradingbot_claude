"""Backtest mit chronologischem Train/Test-Split, Kosten, Buy&Hold-Vergleich.

Look-ahead-Schutz: Signal entsteht am Schlusskurs der Kerze t (nur Daten <= t), Ausführung zum ERÖFFNUNGSKURS
der Kerze t+1 inkl. halber Spread, Slippage und Gebühren. Indikatoren sind kausal. Das Testfenster beginnt mit
leerem Depot; Warm-up-Historie vor dem Fenster fließt nur als Vergangenheit in die Indikatoren ein.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from . import costs as costmod
from . import indicators as ind
from .config import CostsCfg, RiskCfg
from .strategy import Params, add_indicators, decide

ASSUMPTIONS = [
    "Tageskerzen; Signal am Schlusskurs, Ausführung am nächsten Eröffnungskurs.",
    "Kosten: Gebühr (fix/prozentual), halber Spread je Seite, Slippage - siehe Konfiguration.",
    "Long-only, ein Wertpapier je Lauf, keine Hebel, keine Leerverkäufe, keine Steuern, keine Dividenden-/Währungseffekte.",
    "Trailing-Stop wird auf Schlusskursbasis geprüft (kein Intraday, keine Stop-Order); Kurslücken werden nicht abgefangen.",
    "Daten ungeprüft vom Provider; Lücken/ungültige Zeilen werden berichtet, nicht korrigiert. Feiertage nicht modelliert.",
    "Vergangene Ergebnisse sagen nichts über die Zukunft. Strategie ist unvalidiert.",
]


def _size(equity, cash, px, risk: RiskCfg, costs: CostsCfg) -> float:
    room = min(equity * risk.max_position_pct, risk.max_order_value, cash)
    if room < risk.min_order_value:
        return 0.0
    ep = costmod.exec_price("BUY", px, costs)
    q = room / ep
    q = np.floor(q * 10000) / 10000 if risk.allow_fractional else np.floor(q)
    while q > 0 and q * ep + costmod.fee(q * ep, costs) > cash:
        q -= 0.0001 if risk.allow_fractional else 1
    return max(float(q), 0.0)


def simulate(f: pd.DataFrame, p: Params, start: int, end: int, costs: CostsCfg, risk: RiskCfg, cash0: float) -> dict:
    """Simuliert Kerzen [start, end). `f` enthält bereits Indikatoren (kausal)."""
    cash, qty, pending, stop = cash0, 0.0, None, None
    eq, trades, fees, impl = [], [], 0.0, 0.0
    for i in range(start, end):
        row = f.iloc[i]
        if pending:
            side = pending
            ref = row["open"]
            if side == "BUY":
                eq_prev = cash + qty * ref
                q = _size(eq_prev, cash, ref, risk, costs)
                if q > 0:
                    fl = costmod.fill("BUY", q, ref, costs)
                    cash += fl["cash_delta"]; qty += q; fees += fl["fee"]; impl += fl["spread_slip_cost"]
                    trades.append({"date": str(f.index[i].date()), "side": "BUY", "qty": q, "price": fl["price"], "fee": fl["fee"]})
                    stop = None
            elif qty > 0:
                fl = costmod.fill("SELL", qty, ref, costs)
                cash += fl["cash_delta"]; fees += fl["fee"]; impl += fl["spread_slip_cost"]
                trades.append({"date": str(f.index[i].date()), "side": "SELL", "qty": qty, "price": fl["price"], "fee": fl["fee"]})
                qty, stop = 0.0, None
            pending = None
        eq.append(cash + qty * row["close"])
        if qty > 0:
            cand = row["close"] - p.atr_stop_mult * row["atr"]
            if not np.isnan(cand):
                stop = cand if stop is None else max(stop, cand)
        action, _ = decide(row, qty > 0, stop, p)
        if i < end - 1:  # Signal der letzten Kerze hätte keine Folgekerze zur Ausführung
            if action == "BUY" and qty == 0:
                pending = "BUY"
            elif action == "SELL" and qty > 0:
                pending = "SELL"
    s = pd.Series(eq, index=f.index[start:end])
    return _metrics(s, trades, fees, impl, cash0)


def _metrics(s: pd.Series, trades, fees, impl, cash0) -> dict:
    return {
        "start": str(s.index[0].date()), "end": str(s.index[-1].date()), "bars": int(len(s)),
        "total_return": float(s.iloc[-1] / cash0 - 1), "max_drawdown": ind.max_drawdown(s),
        "orders": len(trades), "round_trips": sum(1 for t in trades if t["side"] == "SELL"),
        "fees": float(fees), "spread_slippage_cost": float(impl), "total_costs": float(fees + impl),
        "final_equity": float(s.iloc[-1]),
        "equity_curve": [[str(d.date()), round(float(v), 2)] for d, v in s.iloc[::max(1, len(s) // 300)].items()],
        "trades": trades,
    }


def buy_hold(f: pd.DataFrame, start: int, end: int, costs: CostsCfg, cash0: float) -> dict:
    """Kauft zum ersten Eröffnungskurs des Fensters (mit Kosten) und hält; am Ende kein Verkauf (Marktwert)."""
    ref = f["open"].iloc[start]
    ep = costmod.exec_price("BUY", ref, costs)
    q = cash0 / (ep * (1 + costs.fee_pct)) - costs.fee_fixed / ep
    fl = costmod.fill("BUY", q, ref, costs)
    cash = cash0 + fl["cash_delta"]
    s = cash + q * f["close"].iloc[start:end]
    return _metrics(s, [{"date": str(f.index[start].date()), "side": "BUY", "qty": q, "price": fl["price"], "fee": fl["fee"]}],
                    fl["fee"], fl["spread_slip_cost"], cash0)


def split_index(n: int, train_fraction: float) -> int:
    return int(n * train_fraction)


def run_backtest(df: pd.DataFrame, p: Params, costs: CostsCfg, risk: RiskCfg, train_fraction: float, cash0: float,
                 optimize: bool = False, grid: dict | None = None) -> dict:
    if len(df) < max(p.sma_slow, 60) + 50:
        raise ValueError(f"zu wenig Daten für Backtest ({len(df)} Kerzen)")
    cut = split_index(len(df), train_fraction)
    used = p
    opt = None
    if optimize:
        grid = grid or {"ema_fast": [10, 20, 30], "sma_slow": [50, 100, 150]}
        best = None
        for combo in itertools.product(*grid.values()):
            kw = dict(zip(grid.keys(), combo))
            if kw.get("ema_fast", p.ema_fast) >= kw.get("sma_slow", p.sma_slow):
                continue
            q = Params(**{**p.dict(), **kw})
            r = simulate(add_indicators(df, q), q, q.sma_slow, cut, costs, risk, cash0)  # NUR Trainingsdaten
            if best is None or r["total_return"] > best[0]:
                best = (r["total_return"], q)
        used = best[1]
        opt = {"selected_on": "train", "train_return": best[0], "grid": grid, "selected": used.dict(),
               "warning": "Parameterwahl auf dem Trainingszeitraum; Testzeitraum wurde dafür nicht verwendet. Overfitting-Risiko bleibt."}
    f = add_indicators(df, used)
    warm = max(used.sma_slow, used.rsi_period, used.atr_period) + 1
    out = {
        "params": used.dict(), "optimization": opt, "assumptions": ASSUMPTIONS,
        "split": {"train": [str(df.index[0].date()), str(df.index[cut - 1].date())], "test": [str(df.index[cut].date()), str(df.index[-1].date())]},
        "train": {"strategy": simulate(f, used, warm, cut, costs, risk, cash0), "buy_hold": buy_hold(f, warm, cut, costs, cash0)},
        "test": {"strategy": simulate(f, used, cut, len(df), costs, risk, cash0), "buy_hold": buy_hold(f, cut, len(df), costs, cash0)},
        "unvalidated": True,
    }
    return out
