# Tradebot – Marktanalyse, Backtesting, Paper-Trading, Scalable-MCP (Freigabemodus)

> **Kein Anlagehinweis. Die Beispielstrategie ist UNVALIDIERT.** Es gibt keine Gewinnaussicht. Vergangene
> Backtest-Ergebnisse sagen nichts über die Zukunft. Nutzung auf eigenes Risiko.

Eigenständiger Python-Dienst (FastAPI + SQLite + Scheduler-Thread) mit Web-Dashboard. Er läuft ohne Claude-Chat.

## Verifikationsstatus (bitte zuerst lesen)

| Bereich | Status |
|---|---|
| Indikatoren, Strategie, Backtest, Risikoprüfungen, Paper-Konto, Scheduler/Lock, Neustart-Abgleich, Dashboard | **Implementiert und getestet** (54 Tests, `pytest`; Dashboard im Browser geprüft) |
| Kursdaten über yfinance | Implementiert, **nicht gegen das echte Netz getestet** (Entwicklungsumgebung ohne freien Internetzugang); Tests/Demo nutzen `synthetic` |
| Scalable-MCP: Verbindung, OAuth, Tool-Namen, Parameterschemas | **NICHT verifiziert.** `https://mcp.scalable.capital/mcp` und die Scalable-Hilfeseiten waren aus der Entwicklungsumgebung gesperrt (Egress-Proxy, 403). Es wurden weder echte Tool-Schemas gelesen noch eine Anmeldung durchgeführt. |
| Scalable-MCP-Client-Logik | Getestet **nur gegen einen Fake-Server** (Allowlist, Schema-Prüfung, Sperre von submit/confirm, kein Retry bei unklarer Antwort) |
| Live-Ausführung echter Orders | **Nicht unterstützt und nicht behauptet.** |

Aus Suchergebnissen (nicht aus erster Hand verifiziert): Scalable beschreibt einen zweistufigen Ablauf (Ordervorschau, danach
separate ausdrückliche Bestätigung), Zugriffsstufen (Orders nur mit „Vollzugriff“), Lesetools wie `get_portfolio_overview`,
`get_portfolio_holdings`, `get_portfolio_cash_breakdown`, `list_portfolio_transactions` und ein Vorschau-Tool
(`preview_buy_order`). Ob Anmeldung/Registrierung für eigene Clients (nicht Claude/ChatGPT) möglich ist, ist **unbestätigt**.
Der Bot erfindet daher nichts: Tools werden zur Laufzeit per `tools/list` entdeckt.

## Betriebsarten

1. **PAPER** (Standard, bei jedem Start): vollautomatisch, simuliertes Geld. Signal am Schlusskurs → Order vorgemerkt →
   Fill zum **nächsten Eröffnungskurs** inkl. Gebühr, halbem Spread, Slippage. Alles ist als `SIMULIERT` markiert.
2. **LIVE_APPROVAL**: Der Bot analysiert und erzeugt **nur Ordervorschläge** (Tabelle `proposals`). Er platziert und
   bestätigt **nie** eine Order. Freigabeweg: Vorschlag im **offiziellen Scalable-Ablauf** (Ihr KI-Assistent mit Scalable-Connector
   bzw. Scalable-App) als Ordervorschau erzeugen, prüfen und dort ausdrücklich bestätigen. Danach im Dashboard
   „Extern ausgeführt buchen“ (reine Buchführung, Menge/Preis manuell). Das Dashboard hat **keinen** Bestätigungsknopf,
   weil ein solcher Ablauf für eigene Dienste nicht verifiziert ist. Optional kann der Bot über das konfigurierte
   Vorschau-Tool eine Vorschau anfordern (erzeugt laut Doku keine Order; ungetestet gegen den echten Server).

LIVE_APPROVAL ist gesperrt, bis in `config.yaml` ausdrücklich gesetzt sind: `live.confirmed: true`, `live.budget`,
`live.allowed_symbols` (mit ISIN in `symbols`), `live.max_position_value`, `live.max_order_value`; der Datenprovider darf nicht
`synthetic` sein. Keine Kredite, Hebelprodukte oder Leerverkäufe (nur Long, Verkauf nur aus Bestand).

## Installation

```bash
python3 -m venv .venv && . .venv/bin/activate     # Python >= 3.11
pip install -r requirements.txt
cp config.example.yaml config.yaml                # anpassen; enthält keine Geheimnisse
```

Demo ohne Internet: in `config.yaml` `data.provider: synthetic` (reine Zufallsdaten, im Dashboard deutlich markiert).
Eigene Kurse: `provider: csv` mit `data/csv/<SYMBOL>.csv` (Spalten `date,open,high,low,close,volume`).

## Start

```bash
python -m tradebot serve            # Dashboard http://127.0.0.1:8000 + Scheduler (Dauerbetrieb)
python -m tradebot cycle            # einmaliger Zyklus (z.B. per cron/systemd-timer)
python -m tradebot backtest SPY [--optimize]
python -m tradebot mcp-login        # einmalige offizielle Anmeldung (OAuth im Browser)
python -m tradebot mcp-discover     # Tools + Schemas anzeigen (Klassifikation allowed/blocked)
pytest                              # Tests
```

Das Dashboard bindet an `127.0.0.1`. Schreibende Aktionen verlangen das Token aus `data/secrets/dashboard_token`
(Knopf „Token“). Als Dienst z.B. mit systemd (`ExecStart=/pfad/.venv/bin/python -m tradebot serve`, `Restart=on-failure`).

### Scalable anbinden (Reihenfolge)
1. In Scalable die MCP-Funktion laut Scalable-Hilfe aktivieren (für reine Analyse genügt Lesezugriff).
2. `python -m tradebot mcp-login` → offizielle Anmeldung im Browser. Tokens landen nur in `data/secrets/` (0600, git-ignoriert).
   Erlaubt der Server keine Registrierung eigener Clients, schlägt das fehl – dann bleibt PAPER plus manuelle Nutzung des offiziellen Wegs.
3. `python -m tradebot mcp-discover`, Namen/Schemas prüfen, `scalable.readonly_tools` ggf. anpassen. Für die Vorschau-Option `preview_tool` und
   `preview_arg_map` (Felder `isin`, `side`, `quantity`) setzen; der Bot prüft Argumente gegen das Server-Schema.

## Sicherheits- und Zuverlässigkeitsmechanismen (getestet)
- **Nie aufrufbar**: Tools mit submit/confirm/place/execute/cancel… im Namen (auch nicht per Konfig); nur Allowlist + Vorschau-Tool; keine Browserautomation.
- **Doppelorder-Schutz**: eindeutiger Idempotenzschlüssel je (Symbol, Kerze, Seite); max. ein Signal je Symbol/Kerze/Modus; offene Order/Vorschlag blockiert weitere.
- **Parallele Läufe**: DB-Lease-Lock (auch prozessübergreifend).
- **Reservierungen**: offene Kauforders/Vorschläge reduzieren das verfügbare Guthaben.
- **Daten**: veraltete/fehlende Kurse (`max_staleness_bdays`) → Signal wird protokolliert, aber keine Order. Lücken werden berichtet, nicht korrigiert.
- **Neustart**: Modus zurück auf PAPER; unterbrochene Läufe als `INTERRUPTED`; Vorschau mit unklarem Ausgang → `PREVIEW_UNKNOWN`, **nie automatisch wiederholt**;
  Konsistenzprüfung des Paper-Kontos. Broker-Abgleich: nur lesend per „Depot lesen“, Anzeige roh (Schemas unbekannt), kein automatisches Zusammenführen.
- **Stoppschalter**: blockiert neue Orders/Vorschläge/Simulations-Fills/Vorschau-Anfragen; Daten & Signale laufen weiter (als BLOCKED protokolliert);
  offene Orders bleiben sichtbar. Er storniert **keine** echten Broker-Orders – das geht nur im Scalable-Depot.
- **Protokoll**: Tabellen `signals` (Begründung, Parameter, Datenalter, Ergebnis) und `events`.
- **Nachrichten**: nur Anzeige, bereinigt, unvertraut; fließen nie in Signale/Orders ein. Es wird kein Sprachmodell verwendet; Begründungen sind regelbasierte Texte.

## Was Verlustgrenzen/Stopps wirklich bewirken
Alle Grenzen (Trailing-Stop, Positions-/Ordergrenzen, Drawdown-Halt) werden **nur bei Bot-Läufen** auf Basis der letzten bekannten Kurse
geprüft. Es sind **keine Stop-Orders beim Broker**. Gaps über Nacht, Handelsaussetzungen, Bot-/Datenausfall, Verzögerung bis zur
manuellen Freigabe oder schlechte Ausführung können Verluste über jede Grenze hinaus verursachen. Der Drawdown-Halt verhindert nur *neue Käufe*.

## Strategie (unvalidiert)
Einstieg: Close > SMA(slow) und EMA(fast) > SMA(slow) und RSI < Grenze. Ausstieg: Close < SMA oder EMA < SMA oder Close ≤ Trailing-Stop (Close − k·ATR, nie fallend).
Backtest: chronologisch 60 % Training / 40 % Test, kausale Indikatoren, Ausführung am Folgetag, Kosten, Vergleich mit Buy&Hold, Ausgabe von
Rendite, max. Drawdown, Orderanzahl, Kosten; optionale Parameteroptimierung ausschließlich auf den Trainingsdaten. Annahmen stehen im Ergebnis und im Dashboard.

## Bekannte Grenzen
Nur Tagesdaten; keine Steuern/Dividenden/Währung; interner Live-Bestand nur Näherung; yfinance-Daten sind inoffiziell;
Dashboard ohne Mehrbenutzer-Login; Scalable-Integration siehe Verifikationsstatus.
