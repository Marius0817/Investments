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

## Harte Regeln

- **Orders nie ohne ausdrückliche Bestätigung platzieren.** Die Scalable CLI verlangt eine Vorschau (Phase 1)
  und danach eine separate Zustimmung, bevor `--confirm` (Phase 2) laufen darf
  (`forbid_automatic_phase_2_execution`). Keinen Auto-Confirm-Schalter einbauen – auch nicht in einer UI.
  Eine UI darf Phase 1 anstoßen, muss die Vorschau vollständig anzeigen und Phase 2 erst nach Klick auf
  „Platzieren“ auslösen.
- Unklare Order-Einreichungen nie automatisch wiederholen (`stopguard resolve`).
- Keine echten Depotdaten committen (`.gitignore` deckt Konfiguration, `*.db`, `portfolio.json`, `*.csv` ab).

## UI-Entwicklung

- Gegen den Datenvertrag in `docs/portfolio-data.md` entwickeln (Schema `stopguard.portfolio/1`).
- Beispieldaten: `docs/examples/portfolio-demo.json`. Live-API mit Demodaten:
  ```bash
  pip install -e ".[dev]"
  stopguard demo init ./stopguard-demo
  stopguard -c stopguard-demo/stopguard.toml portfolio sync
  stopguard -c stopguard-demo/stopguard.toml portfolio serve   # http://127.0.0.1:8765/api/v1/…
  ```
- `chart_hints` im Dokument listet sinnvolle Diagramme je Datensatz.
- Renditen sind Dezimalbrüche (0.05 = 5 %), Beträge EUR, `null` = unbekannt (nicht 0).
- `data_quality.warnings` sichtbar anzeigen.
- Fehlt der UI ein Wert, das Feld in `portfolio/analysis.py` bzw. `metrics.py` ergänzen (rückwärtskompatibel)
  und in `docs/portfolio-data.md` dokumentieren – nicht in der UI nachrechnen.

## Tests

```bash
pytest            # alle Tests laufen offline gegen fake_sc
ruff check src tests --select F,E9,B
```
