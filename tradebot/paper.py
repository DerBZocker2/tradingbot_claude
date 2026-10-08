"""Paper-Konto (simuliert). Alles ist ledger-basiert in SQLite; Fills sind atomar (Order+Trade in einer Transaktion).
Alle Datensätze sind simuliert (paper_trades.simulated=1) und tragen nie eine Broker-Referenz."""
from __future__ import annotations

from . import costs as costmod
from .config import Config
from .db import DB, now


class PaperAccount:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db

    def cash(self) -> float:
        r = self.db.one("SELECT COALESCE(SUM(cash_delta),0) s FROM paper_trades")
        return self.cfg.paper.initial_cash + r["s"]

    def reserved(self) -> float:
        r = self.db.one("SELECT COALESCE(SUM(reserved),0) s FROM paper_orders WHERE status='PENDING' AND side='BUY'")
        return r["s"]

    def cash_available(self) -> float:
        return self.cash() - self.reserved()

    def positions(self) -> dict[str, float]:
        rows = self.db.q("SELECT symbol, SUM(CASE side WHEN 'BUY' THEN qty ELSE -qty END) q FROM paper_trades GROUP BY symbol")
        return {r["symbol"]: r["q"] for r in rows if r["q"] and r["q"] > 1e-9}

    def pending_orders(self) -> list[dict]:
        return self.db.q("SELECT * FROM paper_orders WHERE status='PENDING' ORDER BY id")

    def pending_sell_qty(self) -> dict[str, float]:
        return {r["symbol"]: r["q"] for r in self.db.q(
            "SELECT symbol, SUM(qty) q FROM paper_orders WHERE status='PENDING' AND side='SELL' GROUP BY symbol")}

    def entry_bar(self, symbol: str) -> str | None:
        """Signalkerze des letzten Kaufs (für den Trailing-Stop)."""
        r = self.db.one("SELECT signal_bar FROM paper_orders WHERE symbol=? AND side='BUY' AND status='FILLED' ORDER BY id DESC LIMIT 1", (symbol,))
        return r["signal_bar"] if r else None

    def place(self, signal_id: int, symbol: str, side: str, qty: float, ref_price: float, signal_bar: str, note: str) -> int | None:
        """Idempotent: gleicher (Symbol, Kerze, Seite) erzeugt nie zwei Orders."""
        key = f"PAPER:{symbol}:{signal_bar}:{side}"
        reserve = 0.0
        if side == "BUY":
            reserve = -costmod.fill("BUY", qty, ref_price, self.cfg.costs)["cash_delta"] * 1.02  # 2 % Puffer für Gap
        try:
            return self.db.x("INSERT INTO paper_orders(idem_key,signal_id,symbol,side,qty,status,created_ts,signal_bar,reserved,note) VALUES(?,?,?,?,?,'PENDING',?,?,?,?)",
                             (key, signal_id, symbol, side, qty, now(), signal_bar, reserve, note))
        except Exception as e:
            if "UNIQUE" in str(e):
                return None
            raise

    def fill_pending(self, symbol: str, df) -> list[int]:
        """Füllt PENDING-Orders zum Eröffnungskurs der ersten Kerze NACH der Signalkerze."""
        done = []
        for o in [o for o in self.pending_orders() if o["symbol"] == symbol]:
            later = df.loc[df.index > o["signal_bar"]]
            if later.empty:
                continue
            bar = later.iloc[0]
            bar_date = str(later.index[0].date())
            with self.db.tx() as c:
                cur = c.execute("SELECT status FROM paper_orders WHERE id=?", (o["id"],)).fetchone()
                if cur["status"] != "PENDING":
                    continue
                side, qty = o["side"], o["qty"]
                if side == "SELL":
                    held = self.positions().get(symbol, 0.0)
                    if held < qty - 1e-9:
                        c.execute("UPDATE paper_orders SET status='REJECTED', note=? WHERE id=?", (f"Bestand {held} < {qty}", o["id"]))
                        continue
                fl = costmod.fill(side, qty, float(bar["open"]), self.cfg.costs)
                if side == "BUY" and -fl["cash_delta"] > self.cash() + 1e-9:
                    c.execute("UPDATE paper_orders SET status='REJECTED', reserved=0, note='Guthaben reicht bei Ausführung nicht' WHERE id=?", (o["id"],))
                    continue
                c.execute("INSERT INTO paper_trades(order_id,ts,bar_date,symbol,side,qty,price,gross,fee,spread_slip_cost,cash_delta,simulated) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                          (o["id"], now(), bar_date, symbol, side, qty, fl["price"], fl["gross"], fl["fee"], fl["spread_slip_cost"], fl["cash_delta"]))
                c.execute("UPDATE paper_orders SET status='FILLED', reserved=0, fill_bar=? WHERE id=?", (bar_date, o["id"]))
                done.append(o["id"])
        return done

    def equity(self, last_prices: dict[str, float]) -> float:
        return self.cash() + sum(q * last_prices.get(s, 0.0) for s, q in self.positions().items())

    def check_consistency(self) -> list[str]:
        issues = []
        for r in self.db.q("SELECT o.id FROM paper_orders o LEFT JOIN paper_trades t ON t.order_id=o.id WHERE o.status='FILLED' AND t.id IS NULL"):
            issues.append(f"Order {r['id']} FILLED ohne Trade")
        for r in self.db.q("SELECT t.id FROM paper_trades t JOIN paper_orders o ON o.id=t.order_id WHERE o.status!='FILLED'"):
            issues.append(f"Trade {r['id']} ohne FILLED-Order")
        return issues
