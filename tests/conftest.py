import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.config import Config
from tradebot.data import DataService, SyntheticProvider
from tradebot.db import DB
from tradebot.engine import Engine


def make_cfg(tmp_path, **over) -> Config:
    base = {
        "database": str(tmp_path / "t.db"),
        "data": {"provider": "synthetic", "max_staleness_bdays": 3},
        "symbols": [{"symbol": "AAA", "isin": "IE0000000001"}, {"symbol": "BBB", "isin": "IE0000000002"}],
        "scalable": {"token_dir": str(tmp_path / "secrets"), "readonly_tools": ["get_portfolio_overview", "needs_arg"],
                     "preview_tool": "preview_buy_order", "preview_arg_map": {"isin": "isin", "quantity": "quantity", "side": "side"}},
    }
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    return Config(**base)


LIVE_OK = {"confirmed": True, "budget": 5000, "allowed_symbols": ["AAA"], "max_position_value": 2000, "max_order_value": 1500}


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def db(cfg):
    return DB(cfg.path(cfg.database))


@pytest.fixture
def engine(cfg, db):
    return Engine(cfg, db)


def force(monkeypatch, action, reasons=("test",)):
    """Erzwingt das (sonst deterministische) Strategiesignal, um Ablauf/Risiko isoliert zu testen."""
    monkeypatch.setattr("tradebot.engine.decide", lambda row, in_pos, stop, p: (action, list(reasons)))


class FakeSession:
    def __init__(self, tools, log, fail=False):
        self.tools, self.log, self.fail = tools, log, fail

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=n, description=d, inputSchema=s) for n, (d, s) in self.tools.items()])

    async def call_tool(self, name, args):
        self.log.append((name, args))
        if self.fail:
            raise TimeoutError("timeout nach Senden")
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="Vorschau: ok")])


PREVIEW_SCHEMA = {"type": "object", "required": ["isin", "quantity", "side"], "additionalProperties": False,
                  "properties": {"isin": {"type": "string"}, "quantity": {"type": "number"}, "side": {"type": "string"}}}
TOOLS = {
    "get_portfolio_overview": ("Übersicht", {"type": "object", "properties": {}}),
    "needs_arg": ("braucht Argument", {"type": "object", "required": ["portfolio_id"], "properties": {"portfolio_id": {"type": "string"}}}),
    "preview_buy_order": ("Vorschau", PREVIEW_SCHEMA),
    "submit_buy_order": ("SUBMIT", {"type": "object"}),
    "confirm_order": ("CONFIRM", {"type": "object"}),
}


def fake_factory(log, fail=False):
    @asynccontextmanager
    async def f():
        yield FakeSession(TOOLS, log, fail)
    return f
