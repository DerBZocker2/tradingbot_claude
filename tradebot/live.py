"""LIVE_APPROVAL: Der Bot erzeugt NUR Vorschläge (proposals). Er platziert und bestätigt nie Orders.

Ausführung erfolgt ausschließlich über den offiziellen Scalable-Ablauf (Ordervorschau + ausdrückliche Bestätigung
in Ihrem KI-Assistenten/der Scalable-Oberfläche). Der interne Live-Bestand wird aus von Ihnen als 'extern
ausgeführt' markierten Vorschlägen geführt und ist eine Näherung - maßgeblich ist immer das Scalable-Depot.
"""
from __future__ import annotations

import json

from .config import Config
from .db import DB, now

OPEN = ("PROPOSED", "PREVIEW_PENDING", "PREVIEWED", "PREVIEW_UNKNOWN")  # reservieren Budget / blockieren Duplikate


class LiveLedger:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db

    def open_proposals(self) -> list[dict]:
        ph = ",".join("?" * len(OPEN))
        return self.db.q(f"SELECT * FROM proposals WHERE status IN ({ph}) ORDER BY id", OPEN)

    def positions(self) -> dict[str, float]:
        rows = self.db.q("SELECT symbol, SUM(CASE side WHEN 'BUY' THEN exec_qty ELSE -exec_qty END) q FROM proposals WHERE status='EXECUTED_EXTERNALLY' GROUP BY symbol")
        return {r["symbol"]: r["q"] for r in rows if r["q"] and r["q"] > 1e-9}

    def position_cost(self) -> dict[str, float]:
        rows = self.db.q("SELECT symbol, SUM(CASE side WHEN 'BUY' THEN exec_qty*exec_price ELSE -exec_qty*exec_price END) v FROM proposals WHERE status='EXECUTED_EXTERNALLY' GROUP BY symbol")
        return {r["symbol"]: r["v"] for r in rows}

    def reserved(self) -> float:
        return sum(p["est_value"] for p in self.open_proposals() if p["side"] == "BUY")

    def cash_available(self) -> float:
        budget = self.cfg.live.budget or 0.0
        invested = sum(max(v, 0) for v in self.position_cost().values())
        return budget - invested - self.reserved()

    def pending_sell_qty(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.open_proposals():
            if p["side"] == "SELL":
                out[p["symbol"]] = out.get(p["symbol"], 0) + p["qty"]
        return out

    def create(self, signal_id: int, symbol: str, isin: str | None, side: str, qty: int, est_price: float, signal_bar: str, reasons: list[str]) -> int | None:
        key = f"LIVE:{symbol}:{signal_bar}:{side}"
        try:
            return self.db.x("INSERT INTO proposals(idem_key,signal_id,symbol,isin,side,qty,est_price,est_value,status,created_ts,updated_ts,reasons) VALUES(?,?,?,?,?,?,?,?,'PROPOSED',?,?,?)",
                             (key, signal_id, symbol, isin, side, qty, est_price, qty * est_price, now(), now(), json.dumps(reasons)))
        except Exception as e:
            if "UNIQUE" in str(e):
                return None
            raise

    def set_status(self, pid: int, allowed_from: tuple[str, ...], status: str, **fields) -> bool:
        sets = ["status=?", "updated_ts=?"] + [f"{k}=?" for k in fields]
        with self.db.tx() as c:
            cur = c.execute(f"UPDATE proposals SET {', '.join(sets)} WHERE id=? AND status IN ({','.join('?' * len(allowed_from))})",
                            (status, now(), *fields.values(), pid, *allowed_from))
            return cur.rowcount == 1
