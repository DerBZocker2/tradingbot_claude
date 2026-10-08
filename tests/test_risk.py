from tradebot import risk
from tradebot.config import CostsCfg

C = CostsCfg(fee_fixed=0, fee_pct=0.001, fee_min=0, spread_pct=0.001, slippage_pct=0.0005)


def ctx(**kw):
    d = dict(equity=10000, cash_available=10000, peak_equity=10000)
    d.update(kw)
    return risk.Ctx(**d)


def lim(**kw):
    d = dict(max_position_value=2500, max_order_value=2000, min_order_value=50, allow_fractional=False)
    d.update(kw)
    return risk.Limits(**d)


def test_ok_order():
    assert risk.check_order("BUY", "A", 10, 100, ctx(), lim(), C) == []


def test_kill_switch_blocks():
    assert any("Stoppschalter" in v for v in risk.check_order("BUY", "A", 1, 100, ctx(kill_switch=True), lim(), C))
    assert any("Stoppschalter" in v for v in risk.check_order("SELL", "A", 1, 100, ctx(kill_switch=True, positions={"A": 5}), lim(), C))


def test_stale_data_blocks():
    assert any("Datenprüfung" in v for v in risk.check_order("BUY", "A", 1, 100, ctx(data_ok=False, data_msg="alt"), lim(), C))


def test_max_order_and_position():
    assert any("Maximum" in v for v in risk.check_order("BUY", "A", 25, 100, ctx(), lim(max_order_value=2000), C))
    assert any("Positionslimit" in v for v in risk.check_order("BUY", "A", 10, 100, ctx(position_values={"A": 2000}, positions={"A": 20}), lim(), C))


def test_cash_after_reservation():
    assert any("Guthaben" in v for v in risk.check_order("BUY", "A", 10, 100, ctx(cash_available=900), lim(), C))


def test_no_short_selling():
    assert any("Leerverkäufe" in v for v in risk.check_order("SELL", "A", 1, 100, ctx(), lim(), C))
    assert any("Leerverkäufe" in v for v in risk.check_order("SELL", "A", 5, 100, ctx(positions={"A": 5}, pending_sell_qty={"A": 3}), lim(), C))
    assert risk.check_order("SELL", "A", 5, 100, ctx(positions={"A": 5}), lim(), C) == []


def test_allowed_symbols_only():
    assert any("nicht in der Liste" in v for v in risk.check_order("BUY", "X", 1, 100, ctx(), lim(allowed_symbols={"A"}), C))


def test_duplicate_open_order_blocks():
    assert any("Doppelorder" in v for v in risk.check_order("BUY", "A", 1, 100, ctx(open_order_symbols={"A"}), lim(), C))


def test_fractional_rejected_when_not_allowed():
    assert any("Bruchstücke" in v for v in risk.check_order("BUY", "A", 1.5, 100, ctx(), lim(), C))


def test_drawdown_halt_only_blocks_buys():
    c = ctx(equity=8000, peak_equity=10000, positions={"A": 5})
    assert any("Drawdown" in v for v in risk.check_order("BUY", "B", 1, 100, c, lim(drawdown_halt_pct=0.15), C))
    assert risk.check_order("SELL", "A", 5, 100, c, lim(drawdown_halt_pct=0.15), C) == []


def test_max_open_positions():
    c = ctx(positions={"A": 1, "B": 1})
    assert any("offene Positionen" in v for v in risk.check_order("BUY", "C", 1, 100, c, lim(max_open_positions=2), C))


def test_size_buy_respects_all_limits():
    q = risk.size_buy(100, "A", ctx(cash_available=700), lim(), C)
    assert 0 < q <= 6  # Guthaben-Limit inkl. Kosten
    assert risk.size_buy(100, "A", ctx(), lim(max_order_value=500), C) <= 4
    assert risk.size_buy(100, "A", ctx(position_values={"A": 2500}), lim(), C) == 0
