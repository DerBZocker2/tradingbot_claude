"""Risikoprüfungen. Reine Funktionen - jede Verletzung blockiert die Order, es gibt keine Ausnahmen/Overrides.

WICHTIG (tatsächliche Wirkung): Alle Grenzen werden nur geprüft, wenn der Bot einen Lauf macht, auf Basis
der zuletzt bekannten Kurse. Es sind KEINE Broker-seitigen Stop-Orders. Kurslücken (Gaps), Handelsaussetzungen,
Ausfälle des Bots oder verspätete Daten können zu Verlusten über die Grenzen hinaus führen. Es gibt keine Garantie.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import costs as costmod
from .config import CostsCfg


@dataclass
class Limits:
    max_position_value: float
    max_order_value: float
    min_order_value: float = 0.0
    max_open_positions: int = 10**6
    drawdown_halt_pct: float = 1.0
    allow_fractional: bool = False
    allowed_symbols: set[str] | None = None


@dataclass
class Ctx:
    equity: float
    cash_available: float                      # bereits abzüglich reservierter Beträge offener Orders
    positions: dict[str, float] = field(default_factory=dict)       # gehaltene Stückzahl
    position_values: dict[str, float] = field(default_factory=dict)  # aktueller Wert je Position
    pending_sell_qty: dict[str, float] = field(default_factory=dict)  # bereits zum Verkauf vorgemerkt
    open_order_symbols: set[str] = field(default_factory=set)         # Symbole mit offener Order/Vorschlag
    peak_equity: float = 0.0
    kill_switch: bool = False
    data_ok: bool = True
    data_msg: str = ""


def floor_qty(q: float, fractional: bool) -> float:
    return math.floor(q * 10000) / 10000 if fractional else float(math.floor(q))


def size_buy(ref_price: float, symbol: str, ctx: Ctx, lim: Limits, costs: CostsCfg) -> float:
    held_val = ctx.position_values.get(symbol, 0.0)
    room = min(lim.max_position_value - held_val, lim.max_order_value)
    room = min(room, ctx.cash_available)
    if room <= 0 or ref_price <= 0:
        return 0.0
    px = costmod.exec_price("BUY", ref_price, costs)
    q = floor_qty(room / px, lim.allow_fractional)
    while q > 0 and q * px + costmod.fee(q * px, costs) > min(room, ctx.cash_available) + 1e-9:
        q = floor_qty(q - (0.0001 if lim.allow_fractional else 1), lim.allow_fractional)
    return max(q, 0.0)


def check_order(side: str, symbol: str, qty: float, ref_price: float, ctx: Ctx, lim: Limits, costs: CostsCfg) -> list[str]:
    v: list[str] = []
    if ctx.kill_switch:
        v.append("Stoppschalter aktiv")
    if not ctx.data_ok:
        v.append(f"Datenprüfung fehlgeschlagen: {ctx.data_msg}")
    if lim.allowed_symbols is not None and symbol not in lim.allowed_symbols:
        v.append(f"{symbol} nicht in der Liste erlaubter Wertpapiere")
    if side not in ("BUY", "SELL"):
        v.append(f"unbekannte Seite {side}")
        return v
    if not (ref_price and ref_price > 0) or not (qty and qty > 0):
        v.append("ungültiger Preis oder Menge")
        return v
    if not lim.allow_fractional and qty != int(qty):
        v.append("Bruchstücke nicht erlaubt")
    if symbol in ctx.open_order_symbols:
        v.append(f"für {symbol} existiert bereits eine offene Order/Vorschlag (Doppelorder-Schutz)")
    value = qty * ref_price
    if value > lim.max_order_value + 1e-9:
        v.append(f"Ordervolumen {value:.2f} > Maximum {lim.max_order_value:.2f}")
    if side == "BUY":
        if value < lim.min_order_value:
            v.append(f"Ordervolumen {value:.2f} < Minimum {lim.min_order_value:.2f}")
        fill = costmod.fill("BUY", qty, ref_price, costs)
        if -fill["cash_delta"] > ctx.cash_available + 1e-9:
            v.append(f"Kosten {-fill['cash_delta']:.2f} > verfügbares Guthaben {ctx.cash_available:.2f} (nach Reservierungen)")
        after = ctx.position_values.get(symbol, 0.0) + value
        if after > lim.max_position_value + 1e-9:
            v.append(f"Positionswert danach {after:.2f} > Positionslimit {lim.max_position_value:.2f}")
        n_pos = sum(1 for q in ctx.positions.values() if q > 0)
        if ctx.positions.get(symbol, 0) <= 0 and n_pos >= lim.max_open_positions:
            v.append(f"max. offene Positionen ({lim.max_open_positions}) erreicht")
        if ctx.peak_equity > 0 and ctx.equity / ctx.peak_equity - 1 <= -lim.drawdown_halt_pct:
            v.append(f"Konto-Drawdown {ctx.equity / ctx.peak_equity - 1:.1%} überschreitet Halt-Grenze -{lim.drawdown_halt_pct:.0%}")
    else:  # SELL: nur Bestand verkaufen, keine Leerverkäufe
        avail = ctx.positions.get(symbol, 0.0) - ctx.pending_sell_qty.get(symbol, 0.0)
        if qty > avail + 1e-9:
            v.append(f"Verkaufsmenge {qty} > verfügbarer Bestand {avail} (Leerverkäufe sind ausgeschlossen)")
    return v
