"""Anbindung an den offiziellen Scalable-MCP (https://mcp.scalable.capital/mcp).

STATUS DER VERIFIKATION (siehe README, Abschnitt "Verifikationsstatus"):
 - Tool-Namen/Schemas werden NICHT hart codiert angenommen, sondern per tools/list entdeckt und gespeichert.
 - Aufrufbar sind ausschließlich: Tools aus `scalable.readonly_tools` und genau ein optionales Vorschau-Tool.
 - Tools, deren Name auf submit/confirm/place/execute/cancel/order-write hindeutet, sind HART gesperrt (auch wenn
   man sie in die Konfiguration schreibt). Der Bot bestätigt NIE eine Order.
 - Argumente werden gegen das entdeckte JSON-Schema geprüft, bevor ein Aufruf erfolgt.
 - Authentifizierung: MCP-Standard-OAuth (SDK). Ob Scalable beliebige eigene Clients zulässt, ist unbestätigt.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jsonschema

from .config import Config
from .db import DB
from .live import LiveLedger

HARD_BLOCK = re.compile(r"(submit|confirm|place|execute|cancel|amend|modify|delete|transfer|withdraw|deposit|create_order|send_order)", re.I)
SECRET_RE = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|(access|refresh)_token[\"'=: ]+[A-Za-z0-9._~+/=-]+|code=[A-Za-z0-9._~-]+|client_secret[\"'=: ]+\S+)")


def _leaf(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def redact(text) -> str:
    if isinstance(text, BaseException):
        e = _leaf(text)
        text = f"{type(e).__name__}: {e}"
    return SECRET_RE.sub("[REDACTED]", str(text))


class BlockedToolError(Exception):
    pass


class FileTokenStorage:
    """Tokens liegen ausschließlich in data/secrets/ (0600, git-ignoriert) - nie in DB, Logs oder Frontend."""

    def __init__(self, folder: Path):
        self.dir = folder
        self.file = folder / "scalable_oauth.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except Exception:
            return {}

    def _write(self, d: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.file)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        t = self._read().get("tokens")
        return OAuthToken(**t) if t else None

    async def set_tokens(self, tokens) -> None:
        d = self._read(); d["tokens"] = tokens.model_dump(mode="json", exclude_none=True); self._write(d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        c = self._read().get("client")
        return OAuthClientInformationFull(**c) if c else None

    async def set_client_info(self, info) -> None:
        d = self._read(); d["client"] = info.model_dump(mode="json", exclude_none=True); self._write(d)

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))


class ScalableClient:
    def __init__(self, cfg: Config, db: DB, session_factory=None):
        self.cfg, self.db = cfg, db
        self.storage = FileTokenStorage(cfg.path(cfg.scalable.token_dir))
        self._factory = session_factory or self._default_factory

    # ---------- Transport ----------
    @asynccontextmanager
    async def _default_factory(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        auth = self._oauth_provider(interactive=False)
        async with streamablehttp_client(self.cfg.scalable.url, auth=auth, timeout=30) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s

    def _oauth_provider(self, interactive: bool, redirect_handler=None, callback_handler=None):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        port = self.cfg.scalable.oauth_callback_port
        meta = OAuthClientMetadata(client_name="tradebot-personal", redirect_uris=[f"http://localhost:{port}/callback"],
                                   grant_types=["authorization_code", "refresh_token"], response_types=["code"])

        async def no_redirect(url):
            raise BlockedToolError("Anmeldung nötig: `python -m tradebot mcp-login` ausführen (kein interaktiver Login im Dienst).")

        async def no_callback():
            raise BlockedToolError("Anmeldung nötig: `python -m tradebot mcp-login`.")

        return OAuthClientProvider(self.cfg.scalable.url, meta, self.storage,
                                   redirect_handler=redirect_handler if interactive else no_redirect,
                                   callback_handler=callback_handler if interactive else no_callback)

    # ---------- Guards ----------
    def allowed_tools(self) -> set[str]:
        s = set(self.cfg.scalable.readonly_tools)
        if self.cfg.scalable.preview_tool:
            s.add(self.cfg.scalable.preview_tool)
        return s

    def assert_callable(self, name: str, discovered: dict | None = None) -> None:
        if HARD_BLOCK.search(name):
            raise BlockedToolError(f"Tool '{name}' ist hart gesperrt (Bestätigung/Ausführung nur über den offiziellen Ablauf).")
        pv = self.cfg.scalable.preview_tool
        if name == pv and "preview" not in name.lower():
            raise BlockedToolError("preview_tool muss 'preview' im Namen tragen.")
        if name not in self.allowed_tools():
            raise BlockedToolError(f"Tool '{name}' steht nicht auf der Allowlist.")
        if discovered is not None and name not in discovered:
            raise BlockedToolError(f"Tool '{name}' wurde vom Server nicht angeboten (Discovery).")

    # ---------- Discovery ----------
    async def _discover(self) -> dict:
        async with self._factory() as s:
            res = await s.list_tools()
        tools = {t.name: {"description": t.description, "inputSchema": t.inputSchema} for t in res.tools}
        return tools

    def discover(self) -> dict:
        try:
            tools = asyncio.run(self._discover())
        except BaseException as e:
            self.db.set_state("mcp_status", {"ok": False, "ts": time.time(), "error": redact(e)[:500]})
            self.db.log("mcp_error", f"Discovery fehlgeschlagen: {redact(e)[:300]}", level="WARN")
            raise
        self.db.set_state("mcp_tools", tools)
        self.db.set_state("mcp_status", {"ok": True, "ts": time.time(), "error": None, "n_tools": len(tools)})
        self.db.log("mcp_discovery", f"{len(tools)} Tools entdeckt", data={"names": sorted(tools)})
        return tools

    def classify(self, tools: dict) -> dict:
        allowed = self.allowed_tools()
        return {n: ("blocked" if HARD_BLOCK.search(n) else "allowed" if n in allowed else "not_allowlisted") for n in tools}

    # ---------- Aufrufe ----------
    def _validate(self, schema: dict, args: dict) -> None:
        try:
            jsonschema.validate(args, schema or {"type": "object"})
        except jsonschema.ValidationError as e:
            raise BlockedToolError(f"Argumente passen nicht zum Server-Schema: {e.message}")

    async def _call(self, name: str, args: dict):
        async with self._factory() as s:
            return await s.call_tool(name, args)

    def call(self, name: str, args: dict | None = None) -> dict:
        """Ruft genau EINEN Allowlist-Tool einmal auf. Kein automatischer Retry."""
        args = args or {}
        disc = self.db.get_state("mcp_tools") or {}
        self.assert_callable(name, disc)
        self._validate(disc[name].get("inputSchema"), args)
        res = asyncio.run(self._call(name, args))
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        return {"is_error": bool(res.isError), "text": text}

    def read_snapshot(self) -> dict:
        """Ruft die konfigurierten Read-Only-Tools ohne Argumente auf, soweit ihr Schema keine Pflichtfelder hat."""
        disc = self.db.get_state("mcp_tools") or {}
        out = {}
        for n in self.cfg.scalable.readonly_tools:
            if n not in disc:
                out[n] = {"skipped": "vom Server nicht angeboten"}
            elif disc[n].get("inputSchema", {}).get("required"):
                out[n] = {"skipped": f"Pflichtparameter {disc[n]['inputSchema']['required']} - bitte Parameter manuell prüfen"}
            else:
                try:
                    out[n] = self.call(n, {})
                except Exception as e:
                    out[n] = {"error": redact(e)[:300]}
        self.db.set_state("live_snapshot", {"ts": time.time(), "data": out})
        return out

    # ---------- Ordervorschau (keine Order!) ----------
    def request_preview(self, ledger: LiveLedger, pid: int) -> dict:
        if self.db.get_state("kill_switch", False):
            raise BlockedToolError("Stoppschalter aktiv")
        if self.db.get_state("mode", "PAPER") != "LIVE_APPROVAL":
            raise BlockedToolError("Nur im Modus LIVE_APPROVAL")
        sc = self.cfg.scalable
        if not sc.preview_tool or not sc.preview_arg_map:
            raise BlockedToolError("scalable.preview_tool/preview_arg_map nicht konfiguriert - Vorschau bitte im offiziellen Weg anfordern.")
        p = self.db.one("SELECT * FROM proposals WHERE id=?", (pid,))
        if not p:
            raise BlockedToolError("Vorschlag unbekannt")
        values = {"isin": p["isin"], "side": p["side"].lower(), "quantity": p["qty"]}
        unknown = set(sc.preview_arg_map) - set(values)
        if unknown:
            raise BlockedToolError(f"preview_arg_map enthält unbekannte Felder {unknown}")
        args = {theirs: values[ours] for ours, theirs in sc.preview_arg_map.items()}
        disc = self.db.get_state("mcp_tools") or {}
        self.assert_callable(sc.preview_tool, disc)
        self._validate(disc[sc.preview_tool].get("inputSchema"), args)
        # Atomar beanspruchen: verhindert parallele/doppelte Vorschau-Aufrufe
        if not ledger.set_status(pid, ("PROPOSED",), "PREVIEW_PENDING"):
            raise BlockedToolError("Vorschlag ist nicht (mehr) im Status PROPOSED - kein erneuter Aufruf.")
        self.db.log("preview_request", f"Vorschau angefordert für Vorschlag {pid}", p["symbol"], data={"tool": sc.preview_tool})
        try:
            r = self.call(sc.preview_tool, args)
        except BaseException as e:  # unklarer Ausgang -> NICHT wiederholen
            ledger.set_status(pid, ("PREVIEW_PENDING",), "PREVIEW_UNKNOWN", note=redact(f"{type(e).__name__}: {e}")[:300])
            self.db.log("preview_unknown", f"Vorschau-Ausgang unklar für {pid}; kein Retry", p["symbol"], "ERROR")
            raise
        if r["is_error"]:
            ledger.set_status(pid, ("PREVIEW_PENDING",), "PREVIEW_UNKNOWN", note="Server meldete Fehler; kein automatischer Retry", preview=json.dumps(r))
        else:
            ledger.set_status(pid, ("PREVIEW_PENDING",), "PREVIEWED", preview=json.dumps(r))
        self.db.log("preview_response", f"Vorschau-Antwort gespeichert für {pid}", p["symbol"])
        return r

    # ---------- Login (interaktiv, einmalig) ----------
    def login(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import urlparse, parse_qs
        port = self.cfg.scalable.oauth_callback_port
        result: dict = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                result["code"], result["state"] = q.get("code", [None])[0], q.get("state", [None])[0]
                self.send_response(200); self.end_headers()
                self.wfile.write(b"Anmeldung empfangen. Dieses Fenster kann geschlossen werden.")

            def log_message(self, *a):
                pass

        async def redirect(url):
            print("\nBitte im Browser die offizielle Scalable-Anmeldung öffnen:\n" + url + "\n")

        async def callback():
            srv = HTTPServer(("127.0.0.1", port), H)
            await asyncio.get_running_loop().run_in_executor(None, srv.handle_request)
            srv.server_close()
            return result["code"], result["state"]

        async def go():
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            auth = self._oauth_provider(True, redirect, callback)
            async with streamablehttp_client(self.cfg.scalable.url, auth=auth) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return await s.list_tools()

        res = asyncio.run(go())
        self.db.set_state("mcp_tools", {t.name: {"description": t.description, "inputSchema": t.inputSchema} for t in res.tools})
        self.db.set_state("mcp_status", {"ok": True, "ts": time.time(), "error": None, "n_tools": len(res.tools)})
        print(f"Angemeldet. {len(res.tools)} Tools entdeckt.")
