from __future__ import annotations

import argparse
import json
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tradebot")
    sub = ap.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Dienst + Dashboard + Scheduler starten (Standard)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("cycle", help="Einen Analyse-/Paper-Zyklus ausführen")
    b = sub.add_parser("backtest", help="Backtest für ein Symbol")
    b.add_argument("symbol"); b.add_argument("--optimize", action="store_true")
    sub.add_parser("mcp-login", help="Einmalige offizielle Anmeldung beim Scalable-MCP")
    sub.add_parser("mcp-discover", help="Tools und Schemas des Scalable-MCP auflisten")
    a = ap.parse_args(argv)

    from .config import load_config
    cfg = load_config()
    cmd = a.cmd or "serve"
    if cmd == "serve":
        import uvicorn
        from .api import create_app, load_token
        host = getattr(a, "host", "127.0.0.1"); port = getattr(a, "port", 8000)
        print(f"Dashboard: http://{host}:{port}  | Token-Datei: {cfg.path(cfg.scalable.token_dir) / 'dashboard_token'}")
        if host not in ("127.0.0.1", "localhost"):
            print("WARNUNG: Dashboard nicht nur lokal erreichbar - schützen Sie es per Reverse-Proxy/VPN.", file=sys.stderr)
        load_token(cfg)
        uvicorn.run(create_app(cfg), host=host, port=port)
        return
    from .db import DB
    from .engine import Engine
    p = cfg.path(cfg.database); p.parent.mkdir(parents=True, exist_ok=True)
    db = DB(p)
    if cmd == "cycle":
        e = Engine(cfg, db); e.startup()
        print(json.dumps(e.run_cycle(), indent=2, ensure_ascii=False))
    elif cmd == "backtest":
        from .backtest import run_backtest
        from .strategy import params_from_cfg
        e = Engine(cfg, db); e.data.update(a.symbol)
        r = run_backtest(e.data.load(a.symbol), params_from_cfg(cfg.strategy), cfg.costs, cfg.risk, cfg.backtest.train_fraction, cfg.backtest.initial_cash, a.optimize)
        for part in ("train", "test"):
            s, h = r[part]["strategy"], r[part]["buy_hold"]
            print(f"[{part}] {s['start']}..{s['end']}  Strategie: {s['total_return']:+.1%} MaxDD {s['max_drawdown']:.1%} Orders {s['orders']} Kosten {s['total_costs']:.2f}"
                  f"  | Buy&Hold: {h['total_return']:+.1%} MaxDD {h['max_drawdown']:.1%}")
        print("UNVALIDIERTE Strategie. Annahmen:\n- " + "\n- ".join(r["assumptions"]))
    elif cmd in ("mcp-login", "mcp-discover"):
        from .scalable import ScalableClient, redact
        c = ScalableClient(cfg, db)
        try:
            if cmd == "mcp-login":
                c.login()
            else:
                tools = c.discover()
                cls = c.classify(tools)
                for n, t in sorted(tools.items()):
                    print(f"\n== {n}  [{cls[n]}]\n{t['description']}\nschema: {json.dumps(t['inputSchema'])}")
        except BaseException as ex:
            print("MCP-Fehler:", redact(ex), file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
