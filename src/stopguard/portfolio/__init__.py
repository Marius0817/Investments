"""Portfolio-Tracking: IRR je Trade/Position/Depot, Trade-Vergleich, Kredit- und Steuereffekte.

Aufbau (Datenfluss):

    Scalable CLI ──sync──▶ SQLite (store) ◀──import── Notion-/CSV-Export
                               │
                    ledger (Kassenbuch) → lots (FIFO) → credit (Kredittranchen, Zinsen)
                               │                         │
                               └──── tax (Steuermodell) ─┴─▶ metrics → export (JSON) / api (HTTP)

Die Ausgabe ist ein versioniertes, diagrammfertiges JSON-Dokument
(siehe docs/portfolio-data.md), auf dem eine UI aufsetzen kann.
"""
