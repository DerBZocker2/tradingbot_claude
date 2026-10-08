import numpy as np
import pandas as pd

from tradebot import indicators as ind
from tradebot.backtest import run_backtest, simulate, buy_hold, split_index
from tradebot.config import CostsCfg, RiskCfg
from tradebot.data import SyntheticProvider, quality_report
from tradebot.strategy import Params, add_indicators

NO = CostsCfg(fee_fixed=0, fee_pct=0, fee_min=0, spread_pct=0, slippage_pct=0)
COST = CostsCfg()
R = RiskCfg()


def df():
    return SyntheticProvider().fetch("TEST", 4)


def test_no_lookahead_future_change_does_not_alter_past():
    d = df(); p = Params()
    f1 = add_indicators(d, p)
    d2 = d.copy(); d2.iloc[-100:, :4] *= 3  # Zukunft verändern
    f2 = add_indicators(d2, p)
    pd.testing.assert_frame_equal(f1.iloc[:-100], f2.iloc[:-100])
    cut = len(d) - 100
    a = simulate(f1, p, 60, cut, COST, R, 10000)
    b = simulate(f2, p, 60, cut, COST, R, 10000)
    assert a["final_equity"] == b["final_equity"] and a["trades"] == b["trades"]


def test_indicators_are_causal():
    d = df(); p = Params()
    full = add_indicators(d, p)
    part = add_indicators(d.iloc[:500], p)
    pd.testing.assert_frame_equal(full.iloc[:500], part)


def test_split_is_chronological_no_overlap():
    r = run_backtest(df(), Params(), COST, R, 0.6, 10000)
    assert r["split"]["train"][1] < r["split"]["test"][0]
    assert r["train"]["strategy"]["end"] < r["test"]["strategy"]["start"]
    assert r["train"]["strategy"]["start"] <= r["train"]["strategy"]["end"]


def test_optimization_uses_train_only():
    d = df()
    cut = split_index(len(d), 0.6)
    d2 = d.copy(); d2.iloc[cut:, :4] *= np.linspace(1, 5, len(d) - cut).reshape(-1, 1)  # Testdaten ändern
    a = run_backtest(d, Params(), COST, R, 0.6, 10000, optimize=True)
    b = run_backtest(d2, Params(), COST, R, 0.6, 10000, optimize=True)
    assert a["optimization"]["selected"] == b["optimization"]["selected"]


def test_costs_reduce_result_and_are_reported():
    d = df(); f = add_indicators(d, Params())
    a = simulate(f, Params(), 60, len(d), NO, R, 10000)
    b = simulate(f, Params(), 60, len(d), COST, R, 10000)
    assert a["total_costs"] == 0 and b["total_costs"] > 0 and b["orders"] > 0
    assert b["final_equity"] < a["final_equity"]


def test_buy_hold_comparison_present():
    r = run_backtest(df(), Params(), COST, R, 0.6, 10000)
    for part in ("train", "test"):
        assert {"total_return", "max_drawdown", "orders", "total_costs"} <= set(r[part]["strategy"]) <= set(r[part]["strategy"])
        assert r[part]["buy_hold"]["orders"] == 1 and r[part]["buy_hold"]["total_costs"] > 0
    assert r["unvalidated"] and r["assumptions"]


def test_long_only_never_negative_position():
    d = df(); f = add_indicators(d, Params())
    res = simulate(f, Params(), 60, len(d), COST, R, 10000)
    q = 0
    for t in res["trades"]:
        q += t["qty"] if t["side"] == "BUY" else -t["qty"]
        assert q >= -1e-9


def test_rsi_macd_atr_basic_properties():
    d = df()
    r = ind.rsi(d["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert (ind.atr(d).dropna() > 0).all()
    assert abs(ind.macd(d["close"])["macd_hist"].dropna()).max() < d["close"].max()
    assert ind.max_drawdown(d["close"]) <= 0
    s = pd.Series([1, 2, 3, 4, 5.0]); assert ind.sma(s, 3).iloc[-1] == 4


def test_gap_reporting():
    d = df().drop(df().index[100:110])
    rep = quality_report(d, "x")
    assert rep["gaps"] and rep["invalid_rows"] == 0
