"""Nachrichten: nur ergänzende, UNVERTRAUTE Information. Fließt NIE in Signale, Orders oder Prompts ein."""
from __future__ import annotations

import re

from .db import DB, now

POS = {"beats", "surge", "record", "upgrade", "growth", "gains", "rally", "profit", "wins"}
NEG = {"miss", "plunge", "downgrade", "lawsuit", "fraud", "loss", "recall", "cuts", "probe", "falls", "warning"}


def clean(t: str, n: int = 300) -> str:
    t = re.sub(r"<[^>]+>", " ", str(t or ""))
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:n]


def sentiment(text: str) -> float:
    w = re.findall(r"[a-z]+", text.lower())
    if not w:
        return 0.0
    return (sum(x in POS for x in w) - sum(x in NEG for x in w)) / max(1, len(w)) * 10


def fetch_news(db: DB, symbol: str) -> int:
    try:
        import yfinance as yf
        items = yf.Ticker(symbol).news or []
    except Exception as e:
        db.log("news_error", f"{type(e).__name__}", symbol, "WARN")
        return 0
    n = 0
    for it in items[:15]:
        c = it.get("content", it)
        title, summ = clean(c.get("title", "")), clean(c.get("summary", ""))
        src = clean((c.get("provider") or {}).get("displayName", c.get("publisher", "")), 60)
        if not title:
            continue
        try:
            db.x("INSERT INTO news(ts,symbol,title,summary,source,sentiment) VALUES(?,?,?,?,?,?)",
                 (now(), symbol, title, summ, src, round(sentiment(title + " " + summ), 3)))
            n += 1
        except Exception:
            pass
    return n
