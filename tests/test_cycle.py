import json
import threading

from conftest import force, make_cfg, LIVE_OK
from tradebot.db import DB
from tradebot.engine import Engine
from tradebot.paper import PaperAccount


def orders(db, table="paper_orders"):
    return db.q(f"SELECT * FROM {table}")


def test_duplicate_cycle_creates_one_order(engine, db, monkeypatch):
    force(monkeypatch, "BUY")
    engine.run_cycle(); engine.run_cycle(); engine.run_cycle()
    o = orders(db)
    assert len(o) == 2 and {x["symbol"] for x in o} == {"AAA", "BBB"}  # je Symbol genau eine
    assert len(db.q("SELECT * FROM signals")) == 2


def test_place_is_idempotent(engine, db):
    a = engine.paper.place(1, "AAA", "BUY", 1, 100, "2026-01-02", "x")
    b = engine.paper.place(1, "AAA", "BUY", 1, 100, "2026-01-02", "x")
    assert a and b is None and len(orders(db)) == 1


def test_fill_next_open_and_costs_and_once(engine, db, monkeypatch):
    force(monkeypatch, "BUY")
    engine.run_cycle(refresh=True)
    df = engine.data.load("AAA")
    assert engine.paper.fill_pending("AAA", df) == []  # keine Folgekerze -> nicht gefüllt (kein Look-ahead)
    # Folgekerze anfügen
    nxt = df.index[-1] + (df.index[-1] - df.index[-2])
    db.x("INSERT INTO prices VALUES('AAA',?,?,?,?,?,?, 'x')", (str(nxt.date()), 100, 101, 99, 100, 1))
    df = engine.data.load("AAA")
    assert len(engine.paper.fill_pending("AAA", df)) == 1
    assert engine.paper.fill_pending("AAA", df) == []  # nicht doppelt
    t = db.q("SELECT * FROM paper_trades")[0]
    assert t["price"] > 100 and t["fee"] > 0 and t["simulated"] == 1  # Spread+Slippage+Gebühr
    assert db.one("SELECT reserved FROM paper_orders WHERE symbol='AAA'")["reserved"] == 0 and engine.paper.cash() < 10000


def test_reserved_amount_reduces_available(engine, db, monkeypatch):
    force(monkeypatch, "BUY")
    engine.run_cycle()
    assert engine.paper.reserved() > 0
    assert engine.paper.cash_available() < engine.paper.cash()


def test_stale_data_blocks_signal_action(cfg, db, monkeypatch):
    import datetime as dt
    force(monkeypatch, "BUY")
    e = Engine(cfg, db)
    e.run_cycle(today=dt.date.today() + dt.timedelta(days=30))
    assert orders(db) == []
    assert all(s["outcome"] == "BLOCKED" for s in db.q("SELECT * FROM signals"))


def test_missing_prices_blocks(cfg, db, monkeypatch):
    force(monkeypatch, "BUY")
    class Empty:
        name = "empty"
        def fetch(self, s, y): raise RuntimeError("offline")
    from tradebot.data import DataService
    e = Engine(cfg, db, DataService(cfg, db, Empty()))
    e.run_cycle()
    assert orders(db) == []


def test_kill_switch_blocks_everything_and_shows_open_orders(engine, db, monkeypatch):
    force(monkeypatch, "BUY")
    engine.run_cycle()
    n = len(orders(db))
    engine.set_kill(True)
    r = engine.open_orders()
    assert len(r["paper_pending"]) == n  # offene Orders bleiben sichtbar
    db.x("DELETE FROM signals")
    engine.run_cycle()
    assert len(orders(db)) == n
    assert all(s["outcome"] == "BLOCKED" for s in db.q("SELECT * FROM signals"))
    assert db.q("SELECT * FROM paper_trades") == []


def test_parallel_runs_are_prevented(engine, db):
    assert db.acquire_lock("cycle", "other", 60)
    assert engine.run_cycle() == {"skipped": "lock"}
    db.release_lock("cycle", "other")
    assert "skipped" not in engine.run_cycle()


def test_lock_expires(db):
    assert db.acquire_lock("x", "a", -1)
    assert db.acquire_lock("x", "b", 60)
    assert not db.acquire_lock("x", "a", 60)


def test_concurrent_threads_single_lock_holder(db):
    wins = []
    def w(i):
        if db.acquire_lock("y", f"o{i}", 60):
            wins.append(i)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1


def test_restart_keeps_state_and_reconciles(cfg, monkeypatch):
    force(monkeypatch, "BUY")
    db1 = DB(cfg.path(cfg.database)); e1 = Engine(cfg, db1)
    e1.run_cycle()
    pending = len(PaperAccount(cfg, db1).pending_orders())
    # simulierter Absturz: laufender Lauf + unklare Vorschau
    db1.x("INSERT INTO runs(started,status,owner) VALUES('x','RUNNING','dead')")
    db1.x("INSERT INTO proposals(idem_key,symbol,side,qty,est_price,est_value,status,created_ts,updated_ts) VALUES('k','AAA','BUY',1,1,1,'PREVIEW_PENDING','x','x')")
    db1.set_state("mode", "LIVE_APPROVAL")
    db2 = DB(cfg.path(cfg.database)); e2 = Engine(cfg, db2)
    rep = e2.startup()
    assert rep["interrupted_runs"] == 1 and rep["unclear_previews"] == 1
    assert e2.mode == "PAPER"  # Neustart -> immer PAPER
    assert len(e2.paper.pending_orders()) == pending
    assert db2.one("SELECT status FROM proposals")["status"] == "PREVIEW_UNKNOWN"
    e2.run_cycle()
    assert len(orders(db2)) == pending  # kein Duplikat nach Neustart


def test_consistency_check_detects_orphans(engine, db):
    db.x("INSERT INTO paper_orders(idem_key,symbol,side,qty,status,created_ts,signal_bar,reserved) VALUES('z','AAA','BUY',1,'FILLED','x','2026-01-01',0)")
    assert engine.paper.check_consistency()


def test_live_mode_gate(cfg, db):
    e = Engine(cfg, db)
    import pytest
    with pytest.raises(PermissionError) as ex:
        e.set_mode("LIVE_APPROVAL")
    assert "live.budget" in str(ex.value) and e.mode == "PAPER"


def test_live_requires_isin_and_non_synthetic(tmp_path):
    c = make_cfg(tmp_path, live=LIVE_OK)
    assert any("synthetic" in m for m in c.live_missing())
    c = make_cfg(tmp_path, live=LIVE_OK, data={"provider": "csv"}, symbols=[{"symbol": "AAA"}])
    assert any("isin" in m for m in c.live_missing())
    c = make_cfg(tmp_path, live=LIVE_OK, data={"provider": "csv"})
    assert c.live_missing() == []
