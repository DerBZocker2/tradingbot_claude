import json

import pytest

from conftest import LIVE_OK, fake_factory, force, make_cfg
from tradebot.db import DB
from tradebot.engine import Engine
from tradebot.scalable import BlockedToolError, ScalableClient


@pytest.fixture
def live(tmp_path):
    # provider=csv nur zur Konfig-Freigabe; Kurse kommen aus der DB (Synthetic-Provider im DataService)
    cfg = make_cfg(tmp_path, live=LIVE_OK, data={"provider": "csv", "max_staleness_bdays": 3})
    db = DB(cfg.path(cfg.database))
    from tradebot.data import DataService, SyntheticProvider
    e = Engine(cfg, db, DataService(cfg, db, SyntheticProvider()))
    return cfg, db, e


def to_live(e):
    e.set_mode("LIVE_APPROVAL")


def test_live_creates_proposal_not_order(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e)
    e.run_cycle()
    p = db.q("SELECT * FROM proposals")
    assert len(p) == 1 and p[0]["symbol"] == "AAA" and p[0]["status"] == "PROPOSED"  # BBB nicht erlaubt
    assert p[0]["est_value"] <= cfg.live.max_order_value and isinstance(p[0]["qty"], int)
    assert db.q("SELECT * FROM paper_orders") == [] and db.q("SELECT * FROM paper_trades") == []
    e.run_cycle()
    assert len(db.q("SELECT * FROM proposals")) == 1  # keine Duplikate
    assert e.live.reserved() == p[0]["est_value"]  # reserviert Budget


def test_open_proposal_blocks_second_for_same_symbol(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e)
    e.run_cycle()
    db.x("DELETE FROM signals")
    db.x("UPDATE proposals SET idem_key='old'")
    e.run_cycle()
    assert len(db.q("SELECT * FROM proposals")) == 1
    assert "Doppelorder" in db.one("SELECT reasons FROM signals WHERE symbol='AAA'")["reasons"]


def test_sell_proposal_only_from_booked_position(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "SELL"); to_live(e)
    e.run_cycle()
    assert db.q("SELECT * FROM proposals") == []  # kein Bestand -> kein Leerverkauf
    db.x("INSERT INTO proposals(idem_key,symbol,side,qty,est_price,est_value,status,created_ts,updated_ts,exec_qty,exec_price) VALUES('b','AAA','BUY',3,10,30,'EXECUTED_EXTERNALLY','x','x',3,10)")
    db.x("DELETE FROM signals")
    e.run_cycle()
    p = db.q("SELECT * FROM proposals WHERE side='SELL'")
    assert len(p) == 1 and p[0]["qty"] == 3


def test_kill_switch_blocks_proposals(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e); e.set_kill(True)
    e.run_cycle()
    assert db.q("SELECT * FROM proposals") == []


def test_executed_booking_reduces_budget(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e); e.run_cycle()
    p = db.one("SELECT * FROM proposals")
    assert e.live.set_status(p["id"], ("PROPOSED",), "EXECUTED_EXTERNALLY", exec_qty=2, exec_price=100)
    assert e.live.reserved() == 0 and e.live.positions() == {"AAA": 2}
    assert e.live.cash_available() == 5000 - 200
    assert not e.live.set_status(p["id"], ("PROPOSED",), "EXECUTED_EXTERNALLY", exec_qty=9, exec_price=1)  # nicht doppelt buchbar


# ---------- MCP-Guards ----------
def client(live, fail=False):
    cfg, db, e = live
    log = []
    c = ScalableClient(cfg, db, fake_factory(log, fail))
    c.discover()
    return c, log, e, db


def test_discovery_classifies_and_blocks_submit(live):
    c, log, e, db = client(live)
    cls = c.classify(db.get_state("mcp_tools"))
    assert cls["submit_buy_order"] == "blocked" and cls["confirm_order"] == "blocked"
    assert cls["get_portfolio_overview"] == "allowed" and cls["preview_buy_order"] == "allowed"


@pytest.mark.parametrize("name", ["submit_buy_order", "confirm_order", "place_order", "execute_trade", "cancel_order"])
def test_submit_and_confirm_never_callable_even_if_allowlisted(live, name):
    cfg, db, e = live
    cfg.scalable.readonly_tools.append(name)
    c, log, *_ = client(live)
    with pytest.raises(BlockedToolError):
        c.call(name, {})
    assert log == []


def test_non_allowlisted_tool_blocked(live):
    c, log, *_ = client(live)
    with pytest.raises(BlockedToolError):
        c.call("unknown_tool", {})
    assert log == []


def test_schema_validation_before_call(live):
    c, log, *_ = client(live)
    with pytest.raises(BlockedToolError):
        c.call("needs_arg", {})
    assert log == []


def test_preview_flow_calls_only_preview_once(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e); e.run_cycle()
    c, log, *_ = client(live)
    pid = db.one("SELECT id FROM proposals")["id"]
    r = c.request_preview(e.live, pid)
    assert [n for n, _ in log] == ["preview_buy_order"]
    assert log[0][1]["isin"] == "IE0000000001" and log[0][1]["side"] == "buy"
    assert db.one("SELECT status FROM proposals")["status"] == "PREVIEWED"
    with pytest.raises(BlockedToolError):  # zweiter Aufruf -> kein erneuter Server-Call
        c.request_preview(e.live, pid)
    assert len(log) == 1
    assert not any("submit" in n or "confirm" in n for n, _ in log)


def test_unclear_preview_response_no_retry(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e); e.run_cycle()
    c, log, *_ = client(live, fail=True)
    pid = db.one("SELECT id FROM proposals")["id"]
    with pytest.raises(TimeoutError):
        c.request_preview(e.live, pid)
    assert db.one("SELECT status FROM proposals")["status"] == "PREVIEW_UNKNOWN"
    with pytest.raises(BlockedToolError):
        c.request_preview(e.live, pid)
    assert len(log) == 1


def test_preview_blocked_by_kill_switch_and_mode(live, monkeypatch):
    cfg, db, e = live
    force(monkeypatch, "BUY"); to_live(e); e.run_cycle()
    c, log, *_ = client(live)
    pid = db.one("SELECT id FROM proposals")["id"]
    e.set_kill(True)
    with pytest.raises(BlockedToolError):
        c.request_preview(e.live, pid)
    e.set_kill(False); e.set_mode("PAPER")
    with pytest.raises(BlockedToolError):
        c.request_preview(e.live, pid)
    assert log == []


def test_bad_arg_map_rejected_by_server_schema(live, monkeypatch):
    cfg, db, e = live
    cfg.scalable.preview_arg_map = {"isin": "isin", "quantity": "qty_wrong", "side": "side"}
    force(monkeypatch, "BUY"); to_live(e); e.run_cycle()
    c, log, *_ = client(live)
    pid = db.one("SELECT id FROM proposals")["id"]
    with pytest.raises(BlockedToolError):
        c.request_preview(e.live, pid)
    assert log == [] and db.one("SELECT status FROM proposals")["status"] == "PROPOSED"


def test_tokens_file_permissions_and_not_in_db(live, tmp_path):
    import asyncio, os, stat
    from mcp.shared.auth import OAuthToken
    cfg, db, e = live
    c = ScalableClient(cfg, db, fake_factory([]))
    asyncio.run(c.storage.set_tokens(OAuthToken(access_token="SECRET123", token_type="Bearer")))
    mode = stat.S_IMODE(os.stat(c.storage.file).st_mode)
    assert mode == 0o600
    from tradebot.scalable import redact
    assert "SECRET123" not in redact("Authorization: Bearer SECRET123")
    assert "SECRET123" not in json.dumps(db.q("SELECT * FROM state"))
