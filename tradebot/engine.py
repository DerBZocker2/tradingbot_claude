"""Zyklus: Daten -> Signal (deterministisch) -> Risikoprüfung -> PAPER-Order | LIVE-Vorschlag. Alles wird protokolliert."""
from __future__ import annotations

import json
import os
import re
import socket
import uuid
from datetime import date

import pandas as pd

from . import risk
from .config import Config
from .data import DataService
from .db import DB, now
from .live import LiveLedger
from .paper import PaperAccount
from .strategy import add_indicators, decide, params_from_cfg, trailing_stop

OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
MODES = ("PAPER", "LIVE_APPROVAL")


class Engine:
    def __init__(self, cfg: Config, db: DB, data: DataService | None = None):
        self.cfg, self.db = cfg, db
        self.data = data or DataService(cfg, db)
        self.paper = PaperAccount(cfg, db)
        self.live = LiveLedger(cfg, db)
        self.p = params_from_cfg(cfg.strategy)

    # ---------- Betriebszustand ----------
    @property
    def mode(self) -> str:
        return self.db.get_state("mode", "PAPER")

    @property
    def kill(self) -> bool:
        return bool(self.db.get_state("kill_switch", False))

    def set_kill(self, on: bool, who: str = "user") -> None:
        self.db.set_state("kill_switch", on)
        self.db.log("kill_switch", f"Stoppschalter {'AKTIV' if on else 'aufgehoben'} ({who})", level="WARN")

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("Unbekannter Modus")
        if mode == "LIVE_APPROVAL":
            miss = self.cfg.live_missing()
            if miss:
                raise PermissionError("LIVE_APPROVAL gesperrt, es fehlt: " + ", ".join(miss))
        self.db.set_state("mode", mode)
        self.db.log("mode_change", f"Modus -> {mode}", level="WARN")

    def startup(self) -> dict:
        """Abgleich nach (Neu-)Start. Setzt Modus immer auf PAPER zurück; markiert unterbrochene Läufe/Vorschauen."""
        self.db.set_state("mode", "PAPER")
        stale = self.db.q("SELECT id FROM runs WHERE status='RUNNING'")
        for r in stale:
            self.db.x("UPDATE runs SET status='INTERRUPTED', finished=? WHERE id=?", (now(), r["id"]))
        unclear = self.db.q("SELECT id FROM proposals WHERE status='PREVIEW_PENDING'")
        for r in unclear:  # Ausgang unbekannt -> nie automatisch wiederholen
            self.live.set_status(r["id"], ("PREVIEW_PENDING",), "PREVIEW_UNKNOWN", note="Neustart während Vorschau-Anfrage; Ausgang unklar, kein Retry")
        issues = self.paper.check_consistency()
        rep = {"interrupted_runs": len(stale), "unclear_previews": len(unclear), "paper_issues": issues}
        self.db.log("startup_reconcile", "Neustart-Abgleich", level="WARN" if (stale or unclear or issues) else "INFO", data=rep)
        return rep

    # ---------- Hilfen ----------
    def last_prices(self) -> dict[str, float]:
        out = {}
        for s in self.cfg.symbol_map():
            df = self.data.load(s)
            if not df.empty:
                out[s] = float(df["close"].iloc[-1])
        return out

    def paper_ctx(self, prices: dict[str, float], data_ok: bool, msg: str) -> tuple[risk.Ctx, risk.Limits]:
        eq = self.paper.equity(prices)
        peak = max([self.cfg.paper.initial_cash, eq, self.db.get_state("paper_peak", 0.0)])
        self.db.set_state("paper_peak", peak)
        pos = self.paper.positions()
        r = self.cfg.risk
        ctx = risk.Ctx(equity=eq, cash_available=self.paper.cash_available(), positions=pos,
                       position_values={s: q * prices.get(s, 0) for s, q in pos.items()},
                       pending_sell_qty=self.paper.pending_sell_qty(),
                       open_order_symbols={o["symbol"] for o in self.paper.pending_orders()},
                       peak_equity=peak, kill_switch=self.kill, data_ok=data_ok, data_msg=msg)
        lim = risk.Limits(max_position_value=eq * r.max_position_pct, max_order_value=r.max_order_value, min_order_value=r.min_order_value,
                          max_open_positions=r.max_open_positions, drawdown_halt_pct=r.drawdown_halt_pct, allow_fractional=r.allow_fractional)
        return ctx, lim

    def live_ctx(self, prices: dict[str, float], data_ok: bool, msg: str) -> tuple[risk.Ctx, risk.Limits]:
        l = self.cfg.live
        pos = self.live.positions()
        ctx = risk.Ctx(equity=l.budget or 0.0, cash_available=self.live.cash_available(), positions=pos,
                       position_values={s: q * prices.get(s, 0) for s, q in pos.items()},
                       pending_sell_qty=self.live.pending_sell_qty(),
                       open_order_symbols={p["symbol"] for p in self.live.open_proposals()},
                       peak_equity=0.0, kill_switch=self.kill, data_ok=data_ok, data_msg=msg)
        lim = risk.Limits(max_position_value=l.max_position_value or 0, max_order_value=l.max_order_value or 0,
                          min_order_value=self.cfg.risk.min_order_value, max_open_positions=self.cfg.risk.max_open_positions,
                          allow_fractional=False, allowed_symbols=set(l.allowed_symbols))
        return ctx, lim

    # ---------- Zyklus ----------
    def run_cycle(self, today: date | None = None, refresh: bool = True) -> dict:
        if not self.db.acquire_lock("cycle", OWNER, ttl=900):
            self.db.log("cycle_skipped", "Paralleler Lauf aktiv - übersprungen", level="WARN")
            return {"skipped": "lock"}
        run_id = self.db.x("INSERT INTO runs(started,status,owner) VALUES(?,'RUNNING',?)", (now(), OWNER))
        summary = {"mode": self.mode, "kill_switch": self.kill, "symbols": {}}
        try:
            for sym in self.cfg.symbol_map():
                try:
                    summary["symbols"][sym] = self._symbol(sym, today, refresh)
                except Exception as e:
                    self.db.log("symbol_error", f"{type(e).__name__}: {e}", sym, "ERROR")
                    summary["symbols"][sym] = f"Fehler: {type(e).__name__}"
            self.db.x("UPDATE runs SET status='OK', finished=?, note=? WHERE id=?", (now(), json.dumps(summary)[:2000], run_id))
            self.db.set_state("last_cycle", {"ts": now(), "summary": summary})
        except BaseException:
            self.db.x("UPDATE runs SET status='FAILED', finished=? WHERE id=?", (now(), run_id))
            raise
        finally:
            self.db.release_lock("cycle", OWNER)
        return summary

    def _symbol(self, sym: str, today: date | None, refresh: bool) -> str:
        if refresh:
            self.data.update(sym)
        df = self.data.load(sym)
        fresh, msg = self.data.is_fresh(df, today)
        mode = self.mode
        # 1) Paper: vorgemerkte Orders füllen (nur wenn kein Stoppschalter und Kurse gültig)
        if mode == "PAPER" and not self.kill and not df.empty:
            filled = self.paper.fill_pending(sym, df)
            if filled:
                self.db.log("paper_fill", f"{len(filled)} simulierte Order(s) gefüllt", sym, data={"orders": filled})
        if df.empty:
            self._signal(sym, None, "HOLD", None, ["keine Kurse"], None, mode, "BLOCKED", msg)
            return "keine Kurse"
        f = add_indicators(df, self.p)
        row, bar = f.iloc[-1], str(f.index[-1].date())
        from .data import bdays_between
        age = bdays_between(f.index[-1].date(), today or date.today())
        held = (self.paper.positions() if mode == "PAPER" else self.live.positions()).get(sym, 0.0)
        stop = None
        if held > 0:
            since = self.paper.entry_bar(sym) if mode == "PAPER" else None
            stop = trailing_stop(f, since or f.index[max(0, len(f) - 60)], self.p)
        action, reasons = decide(row, held > 0, stop, self.p)
        if not fresh:
            sid = self._signal(sym, bar, action, float(row["close"]), reasons, age, mode, "BLOCKED", msg)
            return f"{action} blockiert: {msg}"
        if self.db.one("SELECT id FROM signals WHERE symbol=? AND bar_date=? AND mode=?", (sym, bar, mode)):
            return "Signal für diese Kerze bereits verarbeitet (idempotent)"
        sid = self._signal(sym, bar, action, float(row["close"]), reasons, age, mode, "PENDING", "")
        if action == "HOLD":
            self._outcome(sid, "NO_ACTION", "")
            return "HOLD"
        px = float(row["close"])
        prices = self.last_prices()
        ctx, lim = (self.paper_ctx if mode == "PAPER" else self.live_ctx)(prices, True, "ok")
        if mode == "LIVE_APPROVAL":
            miss = self.cfg.live_missing()
            if miss:
                self._outcome(sid, "BLOCKED", "Live-Konfiguration unvollständig: " + ", ".join(miss))
                return "Live-Konfiguration unvollständig"
            if sym not in self.cfg.live.allowed_symbols:
                self._outcome(sid, "BLOCKED", f"{sym} nicht erlaubt")
                return "nicht erlaubt"
        if action == "BUY":
            qty = risk.size_buy(px, sym, ctx, lim, self.cfg.costs)
        else:
            qty = held
        viol = risk.check_order(action, sym, qty, px, ctx, lim, self.cfg.costs) if qty > 0 else ["Menge 0 (Limits/Guthaben)"]
        if viol:
            self._outcome(sid, "BLOCKED", "; ".join(viol))
            self.db.log("order_blocked", "; ".join(viol), sym, "WARN", {"signal": sid})
            return "blockiert: " + "; ".join(viol)
        if mode == "PAPER":
            oid = self.paper.place(sid, sym, action, qty, px, bar, "; ".join(reasons))
            self._outcome(sid, "PAPER_ORDER" if oid else "DUPLICATE", f"order {oid}")
            self.db.log("paper_order", f"{action} {qty} (simuliert, Ausführung zum nächsten Eröffnungskurs)", sym, data={"order": oid, "signal": sid})
            return f"{action} Paper-Order {oid}"
        isin = self.cfg.symbol_map()[sym].isin
        pid = self.live.create(sid, sym, isin, action, int(qty), px, bar, reasons)
        self._outcome(sid, "PROPOSAL" if pid else "DUPLICATE", f"proposal {pid}")
        self.db.log("proposal", f"{action} {int(qty)} {sym} als Vorschlag (keine Order!)", sym, data={"proposal": pid, "signal": sid})
        return f"{action} Vorschlag {pid}"

    def _signal(self, sym, bar, action, price, reasons, age, mode, outcome, note) -> int | None:
        if bar is None:
            self.db.log("signal", f"{action}: {note}", sym, "WARN")
            return None
        reasons = reasons + ([f"Blockiert: {note}"] if note else [])
        try:
            return self.db.x("INSERT INTO signals(ts,symbol,bar_date,mode,action,price,reasons,params,data_age_bdays,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (now(), sym, bar, mode, action, price, json.dumps(reasons), json.dumps(self.p.dict()), age, outcome))
        except Exception as e:
            if "UNIQUE" in str(e):
                return self.db.one("SELECT id FROM signals WHERE symbol=? AND bar_date=? AND mode=?", (sym, bar, mode))["id"]
            raise

    def _outcome(self, sid, outcome, note):
        if sid:
            r = self.db.one("SELECT reasons FROM signals WHERE id=?", (sid,))
            reasons = json.loads(r["reasons"]) + ([note] if note else [])
            self.db.x("UPDATE signals SET outcome=?, reasons=? WHERE id=?", (outcome, json.dumps(reasons), sid))

    # ---------- Offene Orders (auch bei aktivem Stoppschalter sichtbar) ----------
    def open_orders(self) -> dict:
        return {"paper_pending": self.paper.pending_orders(), "live_proposals": self.live.open_proposals(),
                "hinweis": "Paper-Orders sind simuliert. Live-Vorschläge sind KEINE Broker-Orders; echte offene Broker-Orders nur im Scalable-Depot einsehbar."}
