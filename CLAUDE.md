# CLAUDE.md – Orientierung im Repo

Python-Paket `stopguard` (nur Standardbibliothek, Python ≥ 3.11) für ein Scalable-Capital-Depot.
Sprache der Nutzeroberfläche, Doku und Kommentare: **Deutsch**.

## Bausteine

- `src/stopguard/` – Stop-Loss-Verwaltung (Phasen-Strategien, IRR-Stops, Trailing). Kern:
  `engine.py` (Analyse/Plan), `executor.py` (Order-Ablauf), `strategy.py`, `finance.py` (XIRR).
- `src/stopguard/portfolio/` – Portfolio-Tracking (IRR, Trade-Vergleich, Kredit, Steuern).
  Datenfluss: `sync` → SQLite `store` → `ledger` → `lots` → `credit` → `tax` → `metrics` → `analysis` → `api`/Export.
- `src/stopguard/sc.py` – einziger Zugriff auf Scalable: die **offizielle Scalable CLI** (`sc … --json`).
  Keine inoffiziellen APIs, kein Scraping.
- `src/stopguard/fake_sc.py` + `demo.py` – simulierte CLI und Demo-Depot für Tests und Entwicklung.
- `src/stopguard/web/` – Weboberfläche (`stopguard web`): `server.py` (Stdlib-HTTP, Token-Anmeldung, CSRF,
  Host-Prüfung, API `/api/v1/…`), `orders.py` (Vorschau → Bestätigung), `static/` (SPA ohne Build).

## Harte Regeln

- **Orders nie ohne ausdrückliche Bestätigung platzieren.** Die Scalable CLI verlangt eine Vorschau (Phase 1)
  und danach eine separate Zustimmung, bevor `--confirm` (Phase 2) laufen darf
  (`forbid_automatic_phase_2_execution`). Keinen Auto-Confirm-Schalter einbauen – auch nicht in einer UI.
  Eine UI darf Phase 1 anstoßen, muss die Vorschau vollständig anzeigen und Phase 2 erst nach Klick auf
  „Platzieren“ auslösen. In der Weboberfläche: `POST orders/preview` → Vorschau-Seite mit Häkchen →
  `POST orders/{id}/confirm` mit `{"confirm": true}`. Diese Trennung nicht aufweichen.
- Unklare Order-Einreichungen nie automatisch wiederholen (`stopguard resolve`).
- Keine echten Depotdaten committen (`.gitignore` deckt Konfiguration, `*.db`, `portfolio.json`, `*.csv` ab).

## Weboberfläche (`src/stopguard/web/static/`)

- Vanilla-JS (ES-Module), kein Framework, kein Build, keine CDN-Ressourcen (CSP `script-src 'self'`,
  keine Inline-Skripte/-Handler). Schriften liegen unter `static/fonts/`.
- Design-System **Amboss**: `css/amboss-tokens.css` + `css/amboss.css` sind übernommen (nur das Google-Fonts-`@import` entfernt);
  Anpassungen gehören in `css/app.css`. Eine primäre Schaltfläche je Seite, `clay` nur für Hervorhebung,
  Ansprache „du“, Fehlermeldungen dreiteilig (was passiert ist, warum, was jetzt).
- Struktur: `js/main.js` (Hash-Router, jede Ansicht in eigenem Container), `js/views/*.js` (je Seite
  `render(main, params, ctx)` → optional Aufräumfunktion), `js/ui.js` (Bausteine), `js/charts.js` (SVG-Diagramme
  mit Tabellenansicht, Tastatur, Tooltip), `js/fmt.js` (deutsche Formate), `js/data.js` (Cache; schreibende
  Anfragen über `data.mutate(…)`, damit nachfolgende Ladevorgänge den neuen Stand sehen).
- HTML nur über das `html`-Template aus `js/dom.js` erzeugen (escaped automatisch), Ereignisse per `on(…)` delegieren.
- Diagramme: Reihenfarben `c1`–`c3`/`deemph` aus `app.css`, nie eine zweite y-Achse, Legende ab zwei Reihen.
- Ausprobieren:
  ```bash
  pip install -e ".[dev]"
  stopguard demo init ./stopguard-demo
  stopguard -c stopguard-demo/stopguard.toml portfolio sync
  stopguard -c stopguard-demo/stopguard.toml web --open        # http://127.0.0.1:8765/
  ```

## Daten für die UI

- Datenvertrag: `docs/portfolio-data.md` (Schema `stopguard.portfolio/1`), Beispiel `docs/examples/portfolio-demo.json`.
- Renditen sind Dezimalbrüche (0.05 = 5 %), Beträge EUR, `null` = unbekannt (nicht 0).
- `data_quality.warnings` sichtbar anzeigen.
- Fehlt der UI ein Wert, das Feld in `portfolio/analysis.py` bzw. `metrics.py` ergänzen (rückwärtskompatibel)
  und in `docs/portfolio-data.md` dokumentieren – nicht in der UI nachrechnen.

## Tests

```bash
pytest            # alle Tests laufen offline gegen fake_sc (inkl. tests/test_web.py: Sicherheit + Order-Ablauf)
ruff check src tests --select F,E9,B
```
