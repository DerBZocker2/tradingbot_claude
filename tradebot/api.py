"""FastAPI-Dashboard-Backend. Bindet standardmäßig nur an 127.0.0.1. Schreibende Endpunkte brauchen das lokale Dashboard-Token."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import backtest as bt
from .config import ROOT, Config, load_config
from .data import DataService
from .db import DB, now
from .engine import Engine
from .news import fetch_news
from .scalable import BlockedToolError, ScalableClient, redact
from .strategy import DISCLAIMER, add_indicators, params_from_cfg

WEB = ROOT / "web"


def load_token(cfg: Config) -> str:
    env = os.environ.get("TRADEBOT_DASHBOARD_TOKEN")
    if env:
        return env
    f = cfg.path(cfg.scalable.token_dir) / "dashboard_token"
    if f.exists():
        return f.read_text().strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    return tok


class Scheduler(threading.Thread):
    def __init__(self, engine: Engine):
        super().__init__(daemon=True)
        self.e, self.stop = engine, threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.e.run_cycle()
            except Exception as ex:
                self.e.db.log("scheduler_error", f"{type(ex).__name__}: {ex}", level="ERROR")
            self.stop.wait(self.e.cfg.scheduler.interval_minutes * 60)


class ModeBody(BaseModel):
    mode: str


class KillBody(BaseModel):
    on: bool


class BtBody(BaseModel):
    symbol: str
    optimize: bool = False


class ExecBody(BaseModel):
    qty: float
    price: float


def create_app(cfg: Config | None = None, start_scheduler: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    db_path = cfg.path(cfg.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(db_path)
    engine = Engine(cfg, db)
    mcp = ScalableClient(cfg, db)
    token = load_token(cfg)
    sched: list[Scheduler] = []

    @asynccontextmanager
    async def lifespan(app):
        engine.startup()
        if start_scheduler and cfg.scheduler.enabled:
            s = Scheduler(engine); s.start(); sched.append(s)
        yield
        for s in sched:
            s.stop.set()

    app = FastAPI(title="Tradebot", lifespan=lifespan)
    app.state.engine, app.state.db, app.state.mcp = engine, db, mcp

    def auth(x_dashboard_token: str = Header(default="")):
        if not hmac.compare_digest(x_dashboard_token, token):
            raise HTTPException(401, "Dashboard-Token fehlt/falsch (Datei data/secrets/dashboard_token)")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/status")
    def status():
        prices = engine.last_prices()
        reports = {s: engine.data.report(s) for s in cfg.symbol_map()}
        mcp_status = db.get_state("mcp_status")
        tools = db.get_state("mcp_tools") or {}
        pa = engine.paper
        return {
            "mode": engine.mode, "kill_switch": engine.kill, "provider": cfg.data.provider,
            "synthetic_data": cfg.data.provider == "synthetic",
            "live_missing": cfg.live_missing(), "strategy_disclaimer": DISCLAIMER,
            "last_cycle": db.get_state("last_cycle"), "data": reports,
            "mcp": {"url": cfg.scalable.url, "token_present": mcp.storage.has_tokens(), "status": mcp_status,
                    "tools": mcp.classify(tools), "preview_tool": cfg.scalable.preview_tool,
                    "order_execution_in_dashboard": False,
                    "note": "Echte Ausführung wird NICHT über dieses Dashboard unterstützt/verifiziert. Vorschläge müssen über den offiziellen Scalable-Ablauf (Vorschau + ausdrückliche Bestätigung) ausgeführt werden."},
            "paper": {"cash": pa.cash(), "reserved": pa.reserved(), "available": pa.cash_available(), "equity": pa.equity(prices),
                      "initial": cfg.paper.initial_cash,
                      "positions": [{"symbol": s, "qty": q, "price": prices.get(s), "value": q * prices.get(s, 0),
                                     "weight": q * prices.get(s, 0) / max(pa.equity(prices), 1e-9)} for s, q in pa.positions().items()]},
            "live": {"budget": cfg.live.budget, "available_internal": engine.live.cash_available(), "reserved": engine.live.reserved(),
                     "positions_internal": engine.live.positions(), "snapshot": db.get_state("live_snapshot")},
            "open_orders": engine.open_orders(),
            "symbols": [s.model_dump() for s in cfg.symbols],
        }

    @app.get("/api/chart/{symbol}")
    def chart(symbol: str, n: int = 260):
        if symbol not in cfg.symbol_map():
            raise HTTPException(404)
        df = engine.data.load(symbol)
        if df.empty:
            return {"rows": []}
        f = add_indicators(df, params_from_cfg(cfg.strategy)).tail(n)
        cols = ["close", "ema_fast", "sma_slow", "sma20", "rsi", "macd", "macd_signal", "atr", "ret", "vol20", "dd"]
        f = f[cols].round(5)
        return {"dates": [str(d.date()) for d in f.index], "series": {c: [None if v != v else float(v) for v in f[c]] for c in cols}}

    @app.get("/api/signals")
    def signals(limit: int = 100):
        rows = db.q("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["reasons"] = json.loads(r["reasons"])
        return rows

    @app.get("/api/paper")
    def paper():
        return {"trades": db.q("SELECT * FROM paper_trades ORDER BY id DESC LIMIT 200"),
                "orders": db.q("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 200"),
                "label": "SIMULIERT - keine echten Broker-Ausführungen"}

    @app.get("/api/proposals")
    def proposals():
        rows = db.q("SELECT * FROM proposals ORDER BY id DESC LIMIT 200")
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    @app.get("/api/events")
    def events(limit: int = 200):
        return db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/news/{symbol}")
    def news(symbol: str):
        return {"label": "Ergänzende, unvertraute Information - beeinflusst weder Signale noch Orders.",
                "items": db.q("SELECT ts,title,summary,source,sentiment FROM news WHERE symbol=? ORDER BY id DESC LIMIT 20", (symbol,))}

    @app.get("/api/backtests")
    def backtests():
        rows = db.q("SELECT * FROM backtests ORDER BY id DESC LIMIT 20")
        for r in rows:
            r["result"] = json.loads(r["result"]); r["params"] = json.loads(r["params"])
        return rows

    # ---- schreibende Endpunkte ----
    @app.post("/api/backtest", dependencies=[Depends(auth)])
    def run_bt(b: BtBody):
        if b.symbol not in cfg.symbol_map():
            raise HTTPException(404)
        df = engine.data.load(b.symbol)
        try:
            res = bt.run_backtest(df, params_from_cfg(cfg.strategy), cfg.costs, cfg.risk, cfg.backtest.train_fraction, cfg.backtest.initial_cash, b.optimize)
        except ValueError as e:
            raise HTTPException(400, str(e))
        res["data_quality"] = engine.data.report(b.symbol)
        res["data_source"] = cfg.data.provider
        db.x("INSERT INTO backtests(ts,symbol,params,result) VALUES(?,?,?,?)", (now(), b.symbol, json.dumps(res["params"]), json.dumps(res)))
        return res

    @app.post("/api/cycle", dependencies=[Depends(auth)])
    def cycle():
        return engine.run_cycle()

    @app.post("/api/news/{symbol}/refresh", dependencies=[Depends(auth)])
    def news_refresh(symbol: str):
        return {"added": fetch_news(db, symbol)}

    @app.post("/api/mode", dependencies=[Depends(auth)])
    def mode(b: ModeBody):
        try:
            engine.set_mode(b.mode)
        except PermissionError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"mode": engine.mode}

    @app.post("/api/kill", dependencies=[Depends(auth)])
    def kill(b: KillBody):
        engine.set_kill(b.on)
        return {"kill_switch": engine.kill, "open_orders": engine.open_orders()}

    @app.post("/api/mcp/discover", dependencies=[Depends(auth)])
    def discover():
        try:
            tools = mcp.discover()
        except BaseException as e:
            raise HTTPException(502, redact(e)[:400])
        return mcp.classify(tools)

    @app.post("/api/mcp/snapshot", dependencies=[Depends(auth)])
    def snapshot():
        try:
            return mcp.read_snapshot()
        except BaseException as e:
            raise HTTPException(502, redact(e)[:400])

    @app.post("/api/proposals/{pid}/preview", dependencies=[Depends(auth)])
    def preview(pid: int):
        try:
            return mcp.request_preview(engine.live, pid)
        except BlockedToolError as e:
            raise HTTPException(409, str(e))
        except BaseException as e:
            raise HTTPException(502, "Vorschau-Ausgang unklar - Status PREVIEW_UNKNOWN, kein automatischer Retry. " + redact(e)[:200])

    @app.post("/api/proposals/{pid}/discard", dependencies=[Depends(auth)])
    def discard(pid: int):
        ok = engine.live.set_status(pid, ("PROPOSED", "PREVIEW_PENDING", "PREVIEWED", "PREVIEW_UNKNOWN"), "DISCARDED")
        if not ok:
            raise HTTPException(409, "Nicht verwerfbar")
        db.log("proposal_discard", f"Vorschlag {pid} verworfen")
        return {"ok": True}

    @app.post("/api/proposals/{pid}/executed", dependencies=[Depends(auth)])
    def executed(pid: int, b: ExecBody):
        """Nur Buchführung: Sie bestätigen, dass Sie den Vorschlag im OFFIZIELLEN Scalable-Ablauf selbst ausgeführt haben."""
        if b.qty <= 0 or b.price <= 0:
            raise HTTPException(400, "Menge/Preis > 0")
        ok = engine.live.set_status(pid, ("PROPOSED", "PREVIEWED", "PREVIEW_UNKNOWN"), "EXECUTED_EXTERNALLY", exec_qty=b.qty, exec_price=b.price)
        if not ok:
            raise HTTPException(409, "Status erlaubt das nicht")
        db.log("proposal_executed_external", f"Vorschlag {pid} als extern ausgeführt gebucht (manuelle Angabe)", data=b.model_dump())
        return {"ok": True}

    return app
