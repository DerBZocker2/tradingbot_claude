from __future__ import annotations

from .config import CostsCfg


def exec_price(side: str, ref: float, c: CostsCfg) -> float:
    """Referenzpreis inkl. halber Spread + Slippage (immer zum Nachteil des Anlegers)."""
    adj = c.spread_pct / 2 + c.slippage_pct
    return ref * (1 + adj) if side == "BUY" else ref * (1 - adj)


def fee(gross: float, c: CostsCfg) -> float:
    return max(c.fee_min, gross * c.fee_pct) + c.fee_fixed


def fill(side: str, qty: float, ref: float, c: CostsCfg) -> dict:
    px = exec_price(side, ref, c)
    gross = qty * px
    f = fee(gross, c)
    impl = abs(px - ref) * qty  # Spread+Slippage in Geld
    cash = -(gross + f) if side == "BUY" else gross - f
    return {"price": px, "gross": gross, "fee": f, "spread_slip_cost": impl, "cash_delta": cash}
