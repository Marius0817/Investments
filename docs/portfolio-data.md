# Portfolio-Daten – Datenvertrag für UI & Integrationen

Dieses Dokument beschreibt das JSON-Dokument, das `stopguard portfolio export` erzeugt
und `stopguard portfolio serve` über HTTP ausliefert. Es ist die **einzige Schnittstelle**,
gegen die eine UI entwickelt werden sollte (die SQLite-Datenbank ist ein Implementierungs-
detail, siehe unten).

- Schema-Kennung: `"schema": "stopguard.portfolio/1"`. Bei inkompatiblen Änderungen
  wird die Versionsnummer erhöht. Neue Felder können jederzeit hinzukommen – UIs sollen
  unbekannte Felder ignorieren.
- Beispiel mit Demodaten: [`docs/examples/portfolio-demo.json`](examples/portfolio-demo.json)

## Konventionen

| Thema | Konvention |
|---|---|
| Beträge | EUR, Zahl mit max. 2 Nachkommastellen (`1234.56`) |
| Kurse | EUR je Stück, max. 4 Nachkommastellen |
| Renditen/Anteile | **Dezimalbruch**: `0.0512` = 5,12 %. IRR/TWR p.a., sofern nicht anders angegeben |
| Datum | ISO `YYYY-MM-DD` (Zeitreihen, Trades), Zeitstempel ISO-8601 UTC (`as_of`, `generated_at`) |
| Monat | `YYYY-MM` |
| Unbekannt | `null` – nie als 0 interpretieren |
| Vorzeichen in `cashflows` | aus Anlegersicht: Abfluss negativ (Kauf, Steuer, Zinsen, Tilgung), Zufluss positiv |

## Struktur (oberste Ebene)

```text
schema, generator, generated_at, as_of, currency
settings      Benchmark, Steuer- und Kreditparameter, Zeitraster
summary       Kennzahlen (Vermögen, IRRs, TWR, Zähler, beste/schlechteste Trades)
positions[]   je Wertpapier (aggregiert über alle Trades/Lots)
trades[]      je Kauf-Lot (FIFO) inkl. Zahlungsströmen, Steuern, Kredit, Benchmark
credit        Kreditübersicht, Stresstest, Kredit-Trades, Saldo-Verlauf, Zinsen je Monat
taxes         Steuer-Jahresübersicht, Freibetrag, latente Steuer
income        Dividenden/Zinserträge je Monat/Jahr/Position, Prognose der nächsten 12 Monate (forecast)
costs         Gebühren, Kreditzinsen, Steuern je Monat/Jahr
allocation    Aufteilung nach Position, Anlageart, Tag, Anlageklasse, Region, Land, Branche, Währung; ETF-Durchschau
activities[]  alle Buchungen des Verrechnungskontos (neueste zuerst)
insights[]    regelbasierte Hinweise (Klumpenrisiko, Kosten, Kredit, Steuern, Performance)
timeseries    Zeitreihen für Diagramme (Depotwert, TWR, Drawdown, IRR-Verlauf, Positionen)
periods       Rendite je Kalenderjahr, TWR-Zusammenfassung
data_quality  Warnungen, Hinweise, Datenquellen, Abgleich mit Scalable
chart_hints[] Vorschläge, welche Daten sich für welche Diagramme eignen
```

### `summary`

| Feld | Bedeutung |
|---|---|
| `securities_value` | Kurswert aller offenen Positionen |
| `cash`, `loan`, `net_worth` | Guthaben, Kredit (Wertpapierkredit), Nettovermögen = Wert + Guthaben − Kredit |
| `cost_basis_open`, `unrealized_pl`, `realized_pl` | Einstand offener Stücke, unrealisierter / realisierter Gewinn |
| `dividends`, `fees`, `interest_paid`, `latent_tax` | Summen über alle Trades; `latent_tax` = Steuer bei Verkauf heute |
| `net_deposits` | Einzahlungen − Auszahlungen (nur Scalable-Konto) |
| `irr.trades` | XIRR über alle Trades vor Steuern |
| `irr.trades_after_tax` | dito nach Steuern (inkl. Vorabpauschale und latenter Steuer) |
| `irr.trades_after_tax_and_interest` | Eigenkapitalsicht: zusätzlich Kreditaufnahme/-tilgung und Zinsen |
| `irr.account` | XIRR der Ein-/Auszahlungen gegen das heutige Nettovermögen (echte Konto-Rendite) |
| `twr.*` | zeitgewichtete Rendite: `since_start`, `annualized`, `ytd`, `one_year`, `benchmark_since_start`, `max_drawdown` |
| `counts` | `positions_open`, `trades`, `trades_open`, `trades_closed`, `trades_credit`, `win_rate_closed` |
| `best_trades[]`, `worst_trades[]` | je `{id, name, irr, profit, status}` (nur Trades ≥ 30 Tage) |

### `trades[]` – Kernobjekt für den Trade-Vergleich

| Feld | Bedeutung |
|---|---|
| `id` | stabil (`L-<Scalable-Transaktions-ID>` bzw. `M-<Import-ID>`) |
| `source` | `scalable` oder Importquelle (`notion`, …) |
| `origin` | `single`, `savings_plan`, `transfer`, `import` – Sparplanausführungen lassen sich so gruppieren |
| `status` | `open`, `partial` (teilverkauft), `closed` |
| `open_date`, `close_date`, `holding_days` | Haltedauer bis Verkauf bzw. heute |
| `units`, `remaining_units`, `buy_price`, `avg_sell_price`, `current_price` | Stück/Kurse |
| `invested` | Kaufbetrag inkl. Kaufgebühren |
| `proceeds`, `current_value`, `dividends` | Verkaufserlöse (nach Gebühren), Wert offener Stücke, Ausschüttungen |
| `realized_pl`, `unrealized_pl`, `profit`, `return_pct` | Gewinn vor Steuern (absolut/relativ) |
| `irr` | XIRR vor Steuern (Asset-Sicht) |
| `taxes` | `{method, sale, dividends, vorabpauschale, latent, total}` |
| `profit_after_tax`, `irr_after_tax` | nach Steuern |
| `credit` | `null` oder Kreditkennzahlen (siehe unten) |
| `benchmark` | `null` oder `{isin, irr, profit, alpha_irr, outperformance}` – gleiche Beträge zu gleichen Zeitpunkten in den Benchmark investiert |
| `notes[]` | Hinweise zur Datenqualität dieses Trades |
| `cashflows[]` | `{date, type, amount}` – alle Zahlungen der Eigenkapitalsicht; `type` ∈ `buy, sell, dividend, valuation, tax, dividend_tax, vap_tax, latent_tax, loan_draw, loan_repayment, interest, interest_tax_saving, loan_outstanding` |

`credit` (nur bei kreditfinanzierten Trades):

| Feld | Bedeutung |
|---|---|
| `financed`, `leverage` | Kreditbetrag (inkl. kapitalisierter Zinsen), Anteil am Kaufbetrag |
| `outstanding` | noch offener Kredit dieses Trades |
| `interest`, `interest_estimated`, `effective_rate` | zugeordnete Zinsen, ob geschätzt, effektiver Zinssatz p.a. |
| `equity_invested` | maximal gebundenes Eigenkapital |
| `equity_irr` | IRR auf das Eigenkapital nach Steuern **und** Zinsen (`null`, wenn fast vollständig fremdfinanziert) |
| `return_on_equity`, `profit_net` | Nettogewinn / Eigenkapital, Nettogewinn nach Steuern und Zinsen |
| `break_even_irr` | Vorsteuer-Rendite, die nötig ist, um die Zinsen zu decken (Zinsen nicht absetzbar) |
| `spread` | `irr` − `effective_rate` |

### `positions[]`

Aggregation aller Trades je ISIN: `units, avg_buy_price, current_price, cost_basis, value,
weight, unrealized_pl, unrealized_pct, realized_pl, dividends, fees, profit, irr,
irr_after_tax, equity_irr, latent_tax, credit_outstanding, interest, first_buy,
holding_days, trades, open_trades`. `stopguard` enthält – falls die Stop-Loss-Verwaltung
für die Position aktiv ist – `{strategy, phase, target_stop, order_id}`.
`performance = {1w, 1m, 3m, ytd, 1y}` ist die reine Kursentwicklung je Zeitraum (ohne Zu-/Verkäufe;
`null`, wenn die Kurshistorie nicht so weit zurückreicht) – Grundlage der Heatmap.

### `credit`

`mode` (`auto` | `flags` | `off`), `loan`, `available_credit_line`, `loan_to_value`,
`interest_total`, `interest_12m` (davon geschätzt: `interest_12m_estimated`), `effective_rate_12m` (nur echte Scalable-Zinsen ÷ Ø Kreditsaldo), `interest_unallocated`,
`overdraft_draws` (Minussalden ohne Kauf, z. B. durch Gebühren/Steuern),
`stress = {utilization, drop_until_limit, assumption}`, `trades[]` (wie `trades[]`),
`balance[] = {date, loan}`, `interest_monthly[] = {month, interest}`.

### `taxes`

`effective_rate`, `rate_formula`, `basiszins` (Jahr → %), `years[]` mit
`{year, gains_stock, gains_other, dividends_taxable, interest_income, vorabpauschale,
taxable, allowance_used, loss_carryforward_stock, loss_carryforward_other, tax_model, tax_actual}`,
`allowance = {annual, remaining_this_year, hint, candidates[]}`, `latent_total`, `disclaimer`.

### `income`, `costs`, `allocation`

- `income.monthly[] = {month, dividends, interest}`, `by_year[] = {year, dividends, growth}`, `by_position[]`, `trailing_12m`, `yield_12m`.
  `growth` = Veränderung gegenüber dem Vorjahr – erst ab dem zweiten vollen Jahr (das erste ist meist angebrochen), sonst `null`.
- `income.forecast` – **Schätzung** der Ausschüttungen der nächsten 12 Monate:
  jede Zahlung der letzten 12 Monate je Stück × heutiger Bestand, ein Jahr später; bei Positionen unter einem Jahr
  ergänzt um den erkannten Rhythmus (monatlich/quartalsweise/halbjährlich).
  `{basis, estimated: true, next_12m, yield_current, yield_on_cost, monthly[] = {month, amount, items[]},
  payments[] = {date, isin, name, amount, gross, per_share, units, based_on, pending, estimated},
  by_position[] = {isin, name, amount, gross, trailing_12m, frequency, frequency_label, payments, yield_on_cost,
  yield_current, confidence}}`. `confidence`: `high` (volles Jahr gehalten), `medium` (Rhythmus aus ≥ 2 Zahlungen),
  `low` (eine einzelne Zahlung).
- `costs.monthly[] = {month, fees, interest, interest_estimated, taxes}`, `by_year[]`
- `costs.ongoing` – laufende Produktkosten (TER aus `[instruments.X] ter`, Prozent p. a.; Einzelaktien 0):
  `{weighted_ter, annual_cost, coverage, by_position[] = {isin, name, value, ter, annual_cost}, missing[],
  projection[] = {years, value_without_costs, value_with_costs, cost_effect}, assumed_return, note}`.
  Die TER steckt im Kurs und erscheint deshalb nicht in `costs.monthly`.
- `allocation.by_position[] = {isin, name, value, weight}`, `by_kind[]`/`by_tag[] = {key, value, weight}`, `credit_financed`
- `allocation.by_asset_class[]`, `by_region[]`, `by_country[]`, `by_sector[]`, `by_currency[]` =
  `{key, value, weight, positions[] = {name, value}}` (Beiträge je Ausprägung). Fonds werden mit den Gewichten aus
  `[instruments.<ISIN>.exposure]` durchschaut; Einzelaktien ohne Angabe über das Länderkürzel der ISIN (Sitz des
  Emittenten). Was sich nicht zuordnen lässt, steht als `"Nicht zugeordnet"`, Restgewichte von Fonds als `"Sonstige"`.
  `coverage = {dimension: Anteil zugeordnet}`, `derived_share = {dimension: Anteil nur über ISIN abgeleitet}`,
  `unassigned = {dimension: [{isin, name, value}]}`.
- `allocation.fund_overlap = {funds[], pairs[] = {a, b, countries, sectors, holdings, similarity, shared_holdings[]}, method}` –
  Ähnlichkeit zweier Fonds: je Land/Branche das kleinere Gewicht, summiert und auf die vollständigere Liste bezogen
  (1 = gleiche Aufteilung); `holdings` = gemeinsamer Anteil der hinterlegten größten Positionen.
- `allocation.look_through = {items[], overlap[], funds_with_holdings, fund_value_covered}` – größte Einzelwerte
  direkt und über Fonds (`exposure.holdings`): `{key, isin, name, direct, via_funds, value, weight, sources[]}`;
  `overlap[]` sind Werte, die direkt *und* über Fonds gehalten werden.

### `activities[]`

Alle Buchungen von Scalable, neueste zuerst: `{id, date, kind, isin, name, units, price, amount, fees, tax, gross,
description}`. `kind`: `deposit, withdrawal, buy, sell, dividend, interest_income, loan_interest, fee, tax,
tax_refund, transfer_in, transfer_out, other`; `amount` ist die Wirkung auf das Konto (+ Zufluss, − Abfluss).
Importierte Notion-Trades sind keine Kontobuchungen und erscheinen nur unter `trades[]`.

### `insights[]`

Regelbasierte Hinweise mit der Zahl dahinter – Beobachtungen, **keine Anlageempfehlungen**:
`{id, level: warn|info|good, category, title, message, link, value}`. `link` ist ein Pfad der Weboberfläche
(z. B. `#/auswertung/diversifikation`). Schwellen: `portfolio.concentration_limit_pct` (Einzelaktie, Standard 20 %),
`portfolio.cash_hint_pct` (Guthaben, Standard 10 %).

### `timeseries`

| Reihe | Inhalt |
|---|---|
| `resolution` | `daily` / `weekly` / `monthly` (Konfiguration `portfolio.resolution`) |
| `source` | Quelle der TWR: `scalable_chart` (Depotverlauf laut Scalable) oder `reconstructed` |
| `portfolio[]` | `{date, value, invested, cash, loan, net_worth, net_deposits}` |
| `positions` | `{ISIN: [[date, value], …]}` – für gestapelte Flächen |
| `twr[]` | `{date, portfolio, benchmark}` – Index, Start = 100 |
| `drawdown[]` | `{date, drawdown}` (≤ 0) |
| `irr[]` | monatlich `{date, irr, irr_after_tax_excl_latent}` – IRR bis zu diesem Zeitpunkt |

### `periods`

`years[] = {year, twr, benchmark, net_deposits, partial}`, `twr_summary` (wie `summary.twr`) und
`months[] = {month, twr, benchmark, partial}` – zeitgewichtete Rendite je Kalendermonat.

### `summary.risk`

Risikokennzahlen aus dem TWR-Index (vergleichbar mit Fonds/Indizes):
`{risk_free, resolution, windows: {since_start, 1y}, best_month, worst_month, note}`. Jedes Fenster:
`{from, to, days, return_cumulative, return_annualised, volatility, sharpe, sortino, max_drawdown, positive_periods,
beta, correlation, tracking_error, benchmark_return_cumulative, benchmark_volatility, benchmark_max_drawdown}`.
Unter 12 Renditepunkten ist ein Fenster `null`; `return_annualised` (und damit Sharpe/Sortino) erst ab 180 Tagen.
Risikofreier Zins: `portfolio.risk_free_pct` (Standard 2 %).

### `taxes.vorabpauschale_preview`

Schätzung der Vorabpauschale des laufenden Jahres (fällig Anfang des Folgejahres, heutiger Kurs als Jahresendkurs):
`{year, due, basiszins, total_vap, total_taxable, tax_before_allowance, allowance_next_year, tax_after_allowance,
by_position[] = {isin, name, vap, taxable, teilfreistellung, tax}, available, note}`. Sie gilt am ersten Werktag des
Folgejahres als zugeflossen und wird mit dem Freistellungsauftrag des **neuen** Jahres verrechnet. Eingebauter
Basiszins bis 2026 (3,20 %), weitere Jahre über `tax.basiszins`.

### `data_quality`

`warnings[]` (z. B. unbekannte Buchungstypen, Bestandsabweichungen, Kreditabweichung),
`notes[]`, `sources` (Anzahl Transaktionen/Kursreihen, `last_sync_at`), `reconciliation`
(rekonstruiertes vs. von Scalable gemeldetes Guthaben/Kredit). **Eine UI sollte Warnungen
sichtbar anzeigen**, da sie die Aussagekraft der Zahlen betreffen.

### `chart_hints[]`

`{dataset, chart, x, y | value | series, title}` – Vorschläge, z. B. Scatter „Haltedauer vs. IRR“
aus `trades`, gestapelte Balken „Kosten je Monat“ aus `costs.monthly`.

## HTTP-API

```bash
stopguard portfolio serve [--host 127.0.0.1] [--port 8765] [--ttl 300] [--allow-origin *]
```

| GET | Antwort |
|---|---|
| `/api/v1/health` | `{ok, schema, as_of, last_sync_at}` |
| `/api/v1/portfolio` | komplettes Dokument |
| `/api/v1/{summary,positions,trades,credit,taxes,income,costs,allocation,timeseries,periods,data_quality,chart_hints,settings}` | Abschnitt |
| `/api/v1/trades?status=open,partial&origin=single&isin=…&credit=1&sort=-irr` | gefilterte Trades |
| `/api/v1/trades/{id}` | ein Trade |
| `/api/v1/positions/{isin}` | Position + `trades[]` + `timeseries` |

`?refresh=1` berechnet neu (aus der lokalen Datenbank, ohne Scalable-Abfrage). Daten von
Scalable holt nur `stopguard portfolio sync` (z. B. per Zeitplan). Die API ist nur lesend,
ohne Authentifizierung und lauscht standardmäßig nur auf `127.0.0.1`.

### Zusätzlich in `stopguard web` (Weboberfläche)

`stopguard web` liefert dieselben Abschnitte unter `/api/v1/…` (mit Anmeldung, siehe README) und außerdem:

| Methode und Pfad | Inhalt |
|---|---|
| `GET /api/v1/targets` | Zielgewichte `{targets: {ISIN: Prozent}, updated_at, positions[]}` (`<state_dir>/targets.json`) |
| `POST /api/v1/targets` | `{targets: {ISIN: Prozent}}` speichern – Summe 100 % (±0,5), 0 entfernt |
| `GET /api/v1/rebalance?amount=500&mode=buy\|full&min_order=25` | Verteilung: `rows[] = {isin, name, value, weight_now, target, buy, sell, tax_estimate, weight_after, …}`, `drift_now`, `drift_after`, `total_buy`, `total_sell`, `tax_estimate` |
| `GET /api/v1/projection?start=&monthly=&years=&return=&volatility=&inflation=&goal=` | Monte-Carlo-Prognose (Prozentwerte in %): `series[] = {month, p10, p25, p50, p75, p90, invested}`, `final`, `goal = {probability, median_months}`, `defaults` aus der eigenen Historie. **`monthly` < 0 = Entnahmeplan** (Entnahme wächst mit der Inflation): zusätzlich `withdrawal = {monthly, depleted_probability, median_months, early_months, withdrawn_median, planned_total}` |
| `GET /api/v1/watchlist` | Einträge mit Kurs, 12-Monats-Verlauf, Veränderung, 52-Wochen-Hoch/-Tief, Abstand zum Zielkurs, Kursalarmen |
| `POST /api/v1/watchlist` | `{isin, target_price?, note?}` hinzufügen/ändern (ISIN wird bei Scalable geprüft) |
| `POST /api/v1/watchlist/{isin}/remove` | entfernen |
| `POST /api/v1/watchlist/{isin}/alert` | `{price}` – Kursalarm bei Scalable anlegen (**keine Order**) |

## Methodik (Kurzfassung)

- **Trades = Lots nach FIFO.** Jeder Kauf (auch jede Sparplanausführung) ist ein Trade; Verkäufe
  werden FIFO zugeordnet (wie steuerlich bei Girosammelverwahrung). Dividenden werden anteilig
  nach gehaltenen Stücken verteilt.
- **IRR = XIRR** (taggenaue Zahlungen, p.a.). Offene Positionen gehen mit dem aktuellen Kurswert
  (bzw. nach Steuern abzüglich latenter Steuer) ein. Kurze Haltedauern ergeben extreme Jahreswerte.
- **TWR** je Periode nach Modified Dietz (`(V1 − V0 − F) / (V0 + w·F)`), verkettet; Quelle ist
  der Depotverlauf von Scalable (`sc broker chart`) oder eine Rekonstruktion aus Kursen.
- **Steuern (Modell):** Abgeltungsteuer + Soli (+ KiSt), Teilfreistellung nach Fondsart,
  Vorabpauschale für thesaurierende Fonds mit Anrechnung beim Verkauf, Verluste mindern die
  Steuer (`tax.loss_offset`). Freibetrag und Verlusttöpfe nur in der Jahresübersicht.
  `tax.method = "actual"` nutzt stattdessen die von Scalable einbehaltenen Steuern.
- **Kredit (auto):** Fortschreibung des Verrechnungskontos; Kauf ins Minus = Kredittranche des
  Trades; Tilgung durch Verkaufserlöse zuerst beim verkauften Trade, sonst FIFO; Einzahlungen
  sind `credit.deposit_earmark_days` für Käufe zweckgebunden; Zinsbelastungen werden nach
  Saldo-Tagen verteilt. Kreditzinsen sind steuerlich nicht abzugsfähig (§ 20 Abs. 9 EStG).
- **Benchmark je Trade:** gleiche Beträge zu gleichen Zeitpunkten in `portfolio.benchmark_isin`.

## Lokale Datenbank (`<state_dir>/portfolio.db`)

SQLite, Tabellen: `transactions` (Rohdaten + Details als JSON), `prices`, `portfolio_chart`,
`holdings`, `position_snapshots`, `account_snapshots`, `manual_lots` (Importe), `meta`.
Nur für Debugging/Export gedacht; die UI sollte das JSON-Dokument verwenden.

## Mit Demodaten entwickeln

```bash
stopguard demo init ./stopguard-demo
stopguard -c stopguard-demo/stopguard.toml portfolio sync
stopguard -c stopguard-demo/stopguard.toml portfolio serve      # http://127.0.0.1:8765/api/v1/summary
```

Die Demo enthält Sparpläne, einen kreditfinanzierten Trade mit monatlichen Sollzinsen,
einen Gewinn- und einen Verlust-Trade (Verlusttopf), eine Dividende mit Steuerabzug und
eine Vorabpauschale.
