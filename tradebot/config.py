from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class SymbolCfg(BaseModel):
    symbol: str
    name: str = ""
    isin: Optional[str] = None


class DataCfg(BaseModel):
    provider: str = "yfinance"
    csv_dir: str = "data/csv"
    history_years: int = 5
    max_staleness_bdays: int = 3


class StrategyCfg(BaseModel):
    ema_fast: int = 20
    sma_slow: int = 50
    rsi_period: int = 14
    rsi_entry_max: float = 70
    atr_period: int = 14
    atr_stop_mult: float = 3.0


class CostsCfg(BaseModel):
    fee_fixed: float = 0.0
    fee_pct: float = 0.0005
    fee_min: float = 0.0
    spread_pct: float = 0.001
    slippage_pct: float = 0.0005


class RiskCfg(BaseModel):
    max_position_pct: float = 0.25
    max_order_value: float = 2500
    min_order_value: float = 50
    max_open_positions: int = 4
    drawdown_halt_pct: float = 0.15
    allow_fractional: bool = True


class PaperCfg(BaseModel):
    initial_cash: float = 10000


class BacktestCfg(BaseModel):
    train_fraction: float = 0.6
    initial_cash: float = 10000


class SchedulerCfg(BaseModel):
    enabled: bool = True
    interval_minutes: int = 30


class LiveCfg(BaseModel):
    confirmed: bool = False
    budget: Optional[float] = None
    allowed_symbols: list[str] = Field(default_factory=list)
    max_position_value: Optional[float] = None
    max_order_value: Optional[float] = None


class ScalableCfg(BaseModel):
    url: str = "https://mcp.scalable.capital/mcp"
    token_dir: str = "data/secrets"
    oauth_callback_port: int = 8765
    readonly_tools: list[str] = Field(default_factory=list)
    preview_tool: Optional[str] = None
    preview_arg_map: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    database: str = "data/tradebot.db"
    data: DataCfg = DataCfg()
    symbols: list[SymbolCfg] = Field(default_factory=list)
    strategy: StrategyCfg = StrategyCfg()
    costs: CostsCfg = CostsCfg()
    risk: RiskCfg = RiskCfg()
    paper: PaperCfg = PaperCfg()
    backtest: BacktestCfg = BacktestCfg()
    scheduler: SchedulerCfg = SchedulerCfg()
    live: LiveCfg = LiveCfg()
    scalable: ScalableCfg = ScalableCfg()

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else ROOT / q

    def symbol_map(self) -> dict[str, SymbolCfg]:
        return {s.symbol: s for s in self.symbols}

    def live_missing(self) -> list[str]:
        """Liste dessen, was für LIVE_APPROVAL noch ausdrücklich konfiguriert werden muss."""
        l, miss = self.live, []
        if not l.confirmed:
            miss.append("live.confirmed=true")
        if not l.budget or l.budget <= 0:
            miss.append("live.budget")
        if not l.allowed_symbols:
            miss.append("live.allowed_symbols")
        else:
            known = self.symbol_map()
            for s in l.allowed_symbols:
                if s not in known:
                    miss.append(f"symbols: {s} fehlt")
                elif not known[s].isin:
                    miss.append(f"symbols[{s}].isin")
        if not l.max_position_value or l.max_position_value <= 0:
            miss.append("live.max_position_value")
        if not l.max_order_value or l.max_order_value <= 0:
            miss.append("live.max_order_value")
        if self.data.provider == "synthetic":
            miss.append("data.provider darf nicht 'synthetic' sein")
        return miss


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("TRADEBOT_CONFIG", ROOT / "config.yaml"))
    if not p.exists():
        p = ROOT / "config.example.yaml"
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config(**raw)
