# stopguard – Stop-Loss-Verwaltung und Portfolio-Tracking für Scalable Capital

`stopguard` hält die Schutz-Orders in deinem Scalable-Capital-Depot aktuell, und zwar nach Regeln, die die App selbst nicht kennt:

- **Stop nach IRR:** Der Stop liegt immer dort, wo die Position noch eine Mindestrendite p.a. bringt. Mit der Zeit steigt er automatisch.
- **Nachziehen statt Veralten:** Steigt ein Papier, zieht der Stop mit, z. B. immer max. 5 % unter dem Hoch. Dafür wird, wo möglich, der native Trailing-Stop des Brokers genutzt.
- **Phasen-Logik:** z. B. „bis 20 % Verlust tolerieren, bis **erstmals** 5 % IRR erreicht sind, danach max. 5 % Verlust“.

Dazu kommen Backtests, Kursalarme, Push-Benachrichtigungen aufs Handy, ein Audit-Log und ein Betriebsmodus für den Server.

Außerdem gibt es ein **Portfolio-Tracking** (`stopguard portfolio …`, Abschnitt 11) als Ersatz für Notion: IRR je Trade, Position und Depot, Trade-Vergleich gegen einen Benchmark, kreditfinanzierte Trades nach Steuern und Scalable-Zinsen sowie diagrammfertige Daten als JSON/API.

Alles zusammen gibt es auch als **Weboberfläche** (`stopguard web`, Abschnitt 12) mit Diagrammen, Trade-Vergleich und dem Bestätigen von Orders im Browser. Sie läuft im Hell- und Dunkelmodus, auf dem Desktop wie auf dem Handy – und als **Mac-App** (Abschnitt 4).

Inspiriert von Portfolio-Trackern wie Parqet, getquin und extraETF kommen hinzu (Abschnitte 13 und 14):
- Dividendenkalender mit Prognose,
- Diversifikation nach Region, Land, Branche und Währung mit ETF-Durchschau,
- Rebalancing-Rechner,
- Vermögensprognose,
- Heatmap und Watchlist mit Kursalarmen,
- automatische Hinweise zum Depot,
- Privatsphäre-Modus,
- Risikokennzahlen und Monatsrenditen,
- laufende Fondskosten (TER),
- ETF-Überschneidungen,
- Entnahmeplan,
- Vorschau auf die Vorabpauschale.

> **Kein Anlageberater.** stopguard führt nur Regeln aus, die du selbst festlegst. Stop-Orders schützen nicht vor Kurslücken. Teste neue Strategien zuerst mit `execution = "watch"` oder in der Demo.

---

## 1. Die Scalable-Schnittstelle und was das für dich bedeutet

Scalable Capital bietet keine allgemeine REST-API an. Der offizielle und unterstützte Weg ist die **Scalable CLI `sc`** (seit August 2026, „Agentic Investing“, [github.com/ScalableCapital/scalable-cli](https://github.com/ScalableCapital/scalable-cli)). stopguard baut ausschließlich darauf auf. Es nutzt kein Scraping, keine inoffiziellen Endpunkte und speichert kein Passwort.

Laut offizieller Doku läuft **jede Order in zwei Schritten**:

1. **Vorschau** mit allen Kosten und Pflichtinformationen,
2. eine **separate, ausdrückliche Bestätigung durch dich**, erst dann wird die Order eingereicht (Bestätigungs-ID 15 Min. gültig).

Die CLI meldet dazu ausdrücklich `forbid_automatic_phase_2_execution: true`. stopguard hält sich daran. **Kein Schalter platziert Orders ohne deine Rückfrage.** Daraus ergibt sich diese Arbeitsteilung:

| Vollautomatisch (alle 30 Min.)                                    | Mit deiner Bestätigung (`stopguard apply`)              |
|--------------------------------------------------------------------|---------------------------------------------------------|
| Kurse, Bestände, Transaktionen und offene Orders abrufen           | Stop-Order anlegen                                      |
| IRR, Phasen und Hochpunkte berechnen, Soll-Stops ableiten          | Stop nachziehen (alte Order stornieren, neue platzieren) |
| **Kursalarme** in der Scalable-App setzen und verschieben          | Wechsel Stop → Trailing-Stop bei Phasenwechsel          |
| Benachrichtigen: „3 Stops sollten nachgezogen werden“, „Stop erreicht“ | Verkauf vorschlagen, wenn das Stop-Niveau unterschritten ist |
| Session am Leben halten, Ablauf ankündigen                         | Order erneuern, bevor sie nach 360 Tagen abläuft        |

**So bleibt die Zahl der Bestätigungen klein:**

- **Natives Trailing:** Besteht eine Phase nur aus `trailing_pct`, legt stopguard beim Broker einen echten Trailing-Stop an (gettex/EIX). Den zieht Scalable selbst nach. Für Problem 2 und 3 bestätigst du dann nur einmal pro Phasenwechsel.
- **Hysterese** (`min_change_pct`): Ein klassischer Stop wird erst nachgezogen, wenn er sich um mindestens 1 % verbessert.
- **Kursalarm-Modus** (`execution = "alert"`): Es gibt keine Order, aber der Alarm in der Scalable-App wandert vollautomatisch mit, und du bekommst eine Push-Nachricht.

---

## 2. Deine drei Anforderungen und wie sie gelöst sind

| Anforderung | Lösung | Eingebaute Strategie |
|---|---|---|
| 1) Stop nach IRR | Stop-Komponente `irr_floor_pct`: Kurs, bei dem die Position genau x % IRR p.a. hätte, berechnet aus deinen echten Käufen, Sparplanraten, Teilverkäufen und Ausschüttungen. Der Wert steigt mit der Zeit. | `irr_lock` |
| 2) Order veraltet | `trailing_pct`: Stop = Hoch seit Phasenbeginn × (1 − 5 %). Bestehende, veraltete Stop-Orders werden erkannt, übernommen und ersetzt. | `trailing_5` |
| 3) 20 % tolerieren bis 5 % IRR, dann max. 5 % | Phasen: „Aufbau“ (`max_loss_pct = 20`), dann „Gewinne sichern“ ab `irr_pct = 5` (+ Mindesthaltedauer) mit `trailing_pct = 5`. Die Phase gilt **ab dem ersten Erreichen dauerhaft**. | `irr_gate` (bzw. `irr_gate_open` ohne Verlustgrenze) |

Beispiel `irr_gate` für eine Aktie, gekauft zu 100 €:

```
Tag   0  Kauf 100 €          Phase „Aufbau“            Stop 80,00 (−20 %)
Tag  45  Kurs 103 €, IRR 27 %  → Phase „Gewinne sichern“  Trailing-Stop 5 % → Stop ≈ 97,85
Tag  90  Kurs 120 €          Broker zieht nach         Stop ≈ 114,00
Tag 120  Kurs 112 €, IRR 41 %  Phase bleibt (sticky)     Stop bleibt 114,00 → ausgelöst
```

**Hinweis zur IRR:** Die IRR ist eine Jahresrendite. Nach wenigen Tagen ist sie extrem, z. B. +1 % in 7 Tagen ≈ 68 % p.a. Deshalb haben die eingebauten Strategien eine Mindesthaltedauer (`min_holding_days = 30` bzw. `90`). Alternativ kannst du `gain_pct` (Kursgewinn) oder `total_return_pct` verwenden oder Bedingungen kombinieren.

---

## 3. In 2 Minuten ausprobieren (Demo, ohne Konto)

Die Demo nutzt ein **simuliertes Depot** (`stopguard.fake_sc`) mit sechs Positionen, die alle drei Anforderungen zeigen.

```bash
git clone <dieses Repo> && cd Investments
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

stopguard demo init                     # legt ./stopguard-demo an
stopguard -c stopguard-demo/stopguard.toml status
stopguard -c stopguard-demo/stopguard.toml explain IE00B4L5Y983   # Rechenweg IRR-Stop
stopguard -c stopguard-demo/stopguard.toml apply                   # Bestätigungs-Ablauf
stopguard -c stopguard-demo/stopguard.toml demo move SAP 6         # Kurs bewegen …
stopguard -c stopguard-demo/stopguard.toml status                  # … und Reaktion ansehen
stopguard -c stopguard-demo/stopguard.toml backtest DE0007164600 -s irr_gate -s trailing_5 --timeline

stopguard -c stopguard-demo/stopguard.toml portfolio sync          # Portfolio-Daten laden …
stopguard -c stopguard-demo/stopguard.toml web --open              # … und im Browser ansehen
```

Beispielausgabe von `status`:

```
Papier                          Stk.    Kurs     G/V  IRR p.a.  Phase                Soll-Stop           Order/Alarm  Abstand  Aktion
Apple Inc. (Demo)                 10  216.00  +19.9%   +191.6%  1/1 Trailing         Trail 5% (≈205.20)  Stop 171.00   -20.8%  anpassen
iShares Core MSCI World (Demo)  31.13 104.20  +15.9%    +26.0%  2/2 Rendite sichern  93.78               keine         -10.0%  anlegen
NVIDIA Corp. (Demo)                5  119.60   -8.1%    -78.8%  1/2 Aufbau           104.15              keine         -12.9%  anlegen
SAP SE (Demo)                      8  214.00   +6.9%    +16.5%  2/2 Gewinne sichern  Trail 5% (≈203.29)  keine          -5.0%  anlegen
Allianz SE (Demo)                  6  352.40  +21.4%    +24.2%  1/1 Trailing         Trail 5% (≈334.78)  kein Alarm     -5.0%  Alarm setzen
```

---

## 4. Installation auf dem MacBook

**Voraussetzungen:** Python ≥ 3.11 (`brew install python@3.13`) und die offizielle Scalable CLI:

```bash
brew tap ScalableCapital/tap
brew trust --formula ScalableCapital/tap/scalable-cli
brew install scalable-cli
```

Im Scalable-Web unter **Profil → Sicherheit → Agentic Investing** die CLI aktivieren.

**stopguard installieren (inkl. launchd-Job alle 30 Min.):**

```bash
./deploy/macos/install.sh          # deinstallieren: ./deploy/macos/install.sh --uninstall
stopguard login                    # startet 'sc login' – Login im Browser selbst abschließen
open -e ~/.config/stopguard/config.toml
stopguard doctor
stopguard status
```

Der Job führt `stopguard run` aus und meldet sich über die Mitteilungszentrale. Mit `brew install terminal-notifier` werden die Mitteilungen schöner. Zum Bestätigen `stopguard apply` im Terminal ausführen oder `deploy/macos/stopguard-apply.command` doppelklicken. Log: `~/Library/Logs/stopguard.log`.

**Als Mac-App** – eigenes Fenster statt Browser-Tab:

```bash
./deploy/macos/build-app.sh        # installiert/aktualisiert stopguard und baut ~/Applications/stopguard.app
```

Dafür braucht es die Xcode Command Line Tools (`xcode-select --install`). Die App startet `stopguard web` im Hintergrund, und zwar nur auf `127.0.0.1`, mit einem frei gewählten Port und einem neuen Zugangsschlüssel bei jedem Start. Sie meldet sich selbst an. Beim Beenden stoppt der Server mit, auch wenn die App abstürzt.

- **Menü:** „Gehe zu“ (⌘1–⌘6), „Beträge ein-/ausblenden“ (⇧⌘B), „Bei Scalable anmelden …“ (öffnet ein Terminal mit `stopguard login`) und „Log anzeigen“.
- **Ohne Konfiguration** bietet die App das Demo-Depot oder die Einrichtung im Terminal an.
- **Dateien:** Exporte fragen nach dem Speicherort, Importe öffnen den Dateidialog.

Der launchd-Job von `install.sh` (Prüfung alle 30 Minuten) ist unabhängig davon. Die App ersetzt ihn nicht, sie ist die Oberfläche dazu. Ein fertiges App-Paket für Apple Silicon und Intel baut auch die CI auf GitHub (Workflow `macos-app`, Artefakt `stopguard-macos.zip`). Da es nicht notariell beglaubigt ist, beim ersten Start im Finder per Rechtsklick → „Öffnen“ starten. Selbst gebaut entfällt das.

**Mac-Besonderheiten:**

- Die Scalable CLI legt ihren Schlüssel auf dem Mac in der **Secure Enclave** ab. Bei gesperrtem Mac kann sie nicht signieren, stopguard überspringt den Lauf dann stillschweigend.
- Ein zugeklappter Mac schickt keine Anfragen. Nach **24 h ohne Anfrage** endet die Session laut Scalable-Doku, z. B. nach einem Wochenende. Danach `stopguard login`. Für Dauerbetrieb eignet sich der Server besser (Abschnitt 7).

---

## 5. Konfiguration

`stopguard init` legt `~/.config/stopguard/config.toml` an, kommentierte Vorlage: [`src/stopguard/data/config.example.toml`](src/stopguard/data/config.example.toml). Mit `stopguard init --from-depot` werden deine aktuellen Positionen gleich übernommen (zunächst als `watch`).

### Positionen

```toml
[[positions]]
isin = "US0378331005"
strategy = "irr_gate"
execution = "order"      # order | alert | watch
protect_pct = 100        # Anteil der Stücke, die abgesichert werden
# entry_date / entry_price nur bei fehlender Historie (z. B. Depotübertrag)
```

Nicht aufgeführte Positionen werden mit `engine.unlisted_positions = "watch"` angezeigt, aber nie verändert.

### Strategien = Phasen

```toml
[strategies.meine]
description = "…"

[[strategies.meine.phases]]          # Phase 1 gilt sofort
name = "Aufbau"
max_loss_pct = 20

[[strategies.meine.phases]]          # aktiv, sobald ERSTMALS erfüllt (sticky)
name = "Gewinne sichern"
activate = { irr_pct = 5, min_holding_days = 30 }
trailing_pct = 5
```

| Stop-Komponente | Bedeutung |
|---|---|
| `max_loss_pct` | Einstandskurs × (1 − x %) |
| `trailing_pct` | Hochpunkt seit Phasenbeginn × (1 − x %) |
| `irr_floor_pct` | Kurs, bei dem die Position genau x % IRR p.a. hätte (steigt mit der Zeit) |
| `lock_gain_pct` | Einstandskurs × (1 + x %), `0` = Break-even |
| `stop_price` | fester Kurs |
| *(keine)* | kein Stop, Verluste werden hingenommen |

Mehrere Komponenten → **der höchste Wert gilt**. Aktivierungsbedingungen (UND-verknüpft): `irr_pct`, `gain_pct`, `total_return_pct`, `min_holding_days`, `price_above`. Mit `sticky = false` fällt eine Phase zurück, wenn ihre Bedingung nicht mehr erfüllt ist.

`order_type`:

- `auto` (Standard): natives Trailing, wenn möglich, sonst Stop-Market.
- `stop`
- `stop-limit` mit `limit_offset_pct`: schützt vor Verkäufen weit unter dem Stop, wird bei starken Lücken aber eventuell nicht ausgeführt.
- `trailing-stop`

### Wichtige Engine-Optionen

| Option | Standard | Wirkung |
|---|---|---|
| `min_change_pct` | 1.0 | Hysterese: Nachziehen erst ab +1 % |
| `only_raise` | true | Ratchet: Stops werden nie gesenkt, auch nicht nach Nachkäufen oder Phasenwechseln |
| `min_distance_pct` | 1.0 | Stop mind. 1 % unter Geldkurs, sonst würde er sofort auslösen |
| `on_breach` | propose_sell | Kurs unter Soll-Stop (ohne Order im Markt) → Verkauf vorschlagen |
| `adopt_existing_orders` | true | vorhandene Stop-Orders übernehmen und aktuell halten |
| `renew_before_days` | 14 | Orders vor Ablauf (360 Tage) erneuern |
| `exit_costs_eur` | 0 | Verkaufskosten in der IRR-Untergrenze berücksichtigen |

---

## 6. Täglicher Ablauf

```bash
stopguard status            # Übersicht (IRR, Phase, Soll-Stop, Order, Abstand, Aktion)
stopguard plan              # was würde geändert und warum
stopguard apply             # jede Order einzeln: Scalable-Vorschau → „ja“ eintippen → platzieren
stopguard explain <ISIN>    # Rechenweg: Zahlungsreihe, IRR, Phasen, Stop-Komponenten
stopguard history           # Audit-Log aller Aktionen
stopguard backtest <ISIN> -s irr_gate -s trailing_5 --timeline
stopguard strategies        # alle Strategien
```

Dasselbe geht in der Weboberfläche (`stopguard web`, Abschnitt 12): Seite **Stops** → „Vorschau von Scalable anfordern“ → prüfen → „Order verbindlich platzieren“.

**Ablauf beim Ersetzen einer Order:** stopguard erstellt zuerst die Vorschau der neuen Order, während die alte noch aktiv ist. Erst nach deinem „ja“ wird die alte storniert und die neue eingereicht. Blockiert die alte Order die Stücke, fragt stopguard, ob zuerst storniert werden soll, und warnt deutlich, solange die Position ungeschützt ist.

**Unklare Einreichungen**, z. B. bei einem Netzwerkfehler in Phase 2, werden gemäß Scalable-Recovery-Prozedur **nie automatisch wiederholt**. Die Position ist dann gesperrt, bis `stopguard resolve` geklärt hat, ob die Order existiert. Ist die Bestätigungs-ID noch gültig, kann sie wiederverwendet werden, der Duplikatschutz bleibt dabei erhalten.

---

## 7. Betrieb auf dem Server

### Docker (empfohlen, z. B. Heimserver, NAS, Raspberry Pi 4/5 mit 64-bit-OS)

Das Image enthält die offizielle Linux-CLI. Sie wird beim Build per **minisign-Signatur und SHA-256** gegen den Scalable-Release-Schlüssel geprüft.

```bash
mkdir config && cp src/stopguard/data/config.example.toml config/stopguard.toml   # anpassen
docker compose build
docker compose run --rm stopguard login     # Device-Login: Link + Code im Browser bestätigen
docker compose run --rm stopguard doctor
docker compose up -d                        # Überwachung (Handelszeiten, sonst Keepalive)
docker compose run --rm stopguard apply     # Orders bestätigen (z. B. per SSH vom Handy)

# Weboberfläche (Abschnitt 12) – Zugangsschlüssel einmalig erzeugen:
echo "STOPGUARD_WEB_TOKEN=$(openssl rand -hex 24)" >> .env
docker compose --profile web up -d          # http://127.0.0.1:8765/
```

Die Weboberfläche ist im Container standardmäßig nur vom Host aus erreichbar (`127.0.0.1:8765`). Fürs Heimnetz änderst du in `docker-compose.yml` die Portfreigabe auf `"8765:8765"` oder setzt einen Reverse-Proxy davor (siehe Abschnitt 12).

Die Session liegt dateibasiert im Volume `stopguard-home`. Token für ntfy/Telegram übergibst du per `.env` als `STOPGUARD_NTFY_TOKEN` bzw. `STOPGUARD_TELEGRAM_TOKEN`.

### systemd ohne Docker

Siehe [`deploy/systemd/`](deploy/systemd/): Ein Service plus Timer, alle 30 Min. Die offiziellen Linux-Binaries brauchen **glibc ≥ 2.38**. Debian 12 und Raspberry Pi OS „Bookworm“ reichen nicht, dann Docker nutzen.

### Session-Regeln der Scalable CLI

- Access-Token 20 Min. (erneuert sich automatisch)
- 24 h Leerlauf-Timeout: stopguard sendet außerhalb der Handelszeit alle 6 h einen Lese-Request als Keepalive.
- **Spätestens nach 7 Tagen** ist ein neuer interaktiver Login nötig. stopguard erinnert dich einen Tag vorher, wenn du dich per `stopguard login` angemeldet hast.

Für zusätzliche Isolation auf dem Server kann `sc` unter einem eigenen Unix-Nutzer laufen (`sc_command = ["sudo", "-n", "-u", "scalable-cli-user", "/usr/local/bin/sc"]`, siehe Scalable-Doku „Run as a dedicated Unix user“). Mit `[trade_controls]` in der `sc`-Konfiguration lässt sich der Handel zusätzlich auf bestimmte ISINs und Maximalvolumen begrenzen.

---

## 8. Benachrichtigungen

```toml
[notify]
channels = ["console", "macos", "ntfy"]
ntfy_url = "https://ntfy.sh/dein-langes-geheimes-topic"   # App „ntfy“ aufs Handy, Topic abonnieren
# telegram_chat_id = "123456789"                         # + Umgebungsvariable STOPGUARD_TELEGRAM_TOKEN
remind_after_hours = 24
```

`stopguard notify-test` prüft die Kanäle. Gleiche Meldungen werden dedupliziert, dringende (Stop erreicht, Position ungeschützt) wiederholen sich frühestens nach 6 h.

---

## 9. Grenzen und Risiken

- **Kurslücken:** Ein Stop-Market verkauft nach Auslösung zum nächsten Kurs, der deutlich unter dem Stop liegen kann. Ein Stop-Limit wird eventuell gar nicht ausgeführt.
- **Zwischen zwei Läufen** (30 Min.) wird ein klassischer Stop nicht nachgezogen. Native Trailing-Stops zieht der Broker laufend nach.
- **Datenbasis der IRR:** Käufe, Verkäufe, Sparpläne und Ausschüttungen aus `sc broker transactions`. Bei Depotüberträgen fehlt der Kaufkurs. Dann rechnet stopguard eine Näherung mit dem Einstandskurs und markiert sie mit `*`, oder du setzt `entry_date`/`entry_price`. Gebühren sind enthalten, soweit Scalable sie im Transaktionsbetrag ausweist. Steuern bleiben unberücksichtigt.
- **Bruchstücke** aus Sparplänen werden mit der exakten Stückzahl abgesichert. Ob ein Handelsplatz Bruchstücke im Stop akzeptiert, prüft die Vorschau.
- **Mehrere Verkaufsorders** für ein Papier (z. B. zusätzlich ein Take-Profit-Limit) → stopguard verändert dort nichts und meldet den Konflikt.
- Die JSON-Ausgaben der Scalable CLI sind laut Scalable nicht garantiert stabil. stopguard ist gegen CLI **1.3.0** entwickelt. Vor CLI-Updates die Release-Notes prüfen und `stopguard doctor` ausführen.

---

## 10. Entwicklung

```bash
pip install -e ".[dev]"
pytest                    # 94 Tests: XIRR (Excel-Referenz), Phasen, Planung, Order-Ablauf, Steuern, Kredit, Portfolio-API, Weboberfläche, Dividenden, Diversifikation, Rebalancing, Watchlist
```

| Modul | Aufgabe |
|---|---|
| `sc.py` | Wrapper um `sc … --json`: Fehlercodes, Rate-Limits, Order-Spezifikation |
| `finance.py` | XIRR, IRR-Untergrenze (geschlossene Formel), Zahlungsreihen aus Transaktionen |
| `strategy.py` | Phasenwechsel und Stop-Komponenten (pure Funktionen, auch für den Backtest) |
| `engine.py` | Analyse aller Positionen, Abgleich Soll/Ist, Maßnahmenplan |
| `executor.py` | Zweistufiger Order-Ablauf mit Bestätigung, Recovery |
| `state.py` | Zustand (Phasen, Hochpunkte, verwaltete Orders), Audit-Log, Prozess-Sperre |
| `fake_sc.py` | Simulierte Scalable CLI für Demo und Tests |
| `portfolio/` | Portfolio-Tracking: `store` (SQLite), `sync`, `ledger` (Kassenbuch), `lots` (FIFO), `credit`, `tax`, `metrics`, `analysis`, `api`, `importers`, `exposure` (Diversifikation/Durchschau), `dividends` (Prognose), `insights` (Hinweise), `planning` (Rebalancing, Prognose, Entnahmeplan), `watchlist`, `risk` (Risikokennzahlen, Monatsrenditen), `ongoing` (TER) |
| `web/` | Weboberfläche: `server.py` (HTTP, Anmeldung, CSRF-Schutz, API), `orders.py` (zweistufiger Order-Ablauf), `static/` (Single-Page-App ohne Build-Schritt, Design-System Amboss) |
| `deploy/macos/app/` | Mac-App: `main.swift` (Fenster, Menü, Serverprozess, Selbsttest), `Info.plist`, Icon; gebaut von `deploy/macos/build-app.sh` |

Die Oberfläche ist reines HTML/CSS/JavaScript (ES-Module) ohne Framework und ohne Build: Datei ändern, Seite neu laden. Zum Ausprobieren eignet sich das Demo-Depot (`stopguard demo init`, dann `portfolio sync` und `web`).

---

## 11. Portfolio-Tracking (Ersatz für die Notion-Tabellen)

Die Scalable-App zeigt keine IRR, keinen Trade-Vergleich und rechnet Kredit- und Steuereffekte nicht ein. `stopguard portfolio` schließt diese Lücken:

| Problem | Lösung |
|---|---|
| Keine IRR | XIRR **je Trade, je Position und fürs Depot**, jeweils vor Steuern, nach Steuern und – bei Kredit – auf das Eigenkapital. Dazu IRR des Kontos (Ein-/Auszahlungen gegen Nettovermögen), zeitgewichtete Rendite (TWR), IRR-Verlauf und Drawdown. |
| Kein Trade-Vergleich | Jeder Kauf ist ein Trade (FIFO wie beim Finanzamt) mit Haltedauer, Gewinn, IRR vor und nach Steuern sowie **Benchmark-IRR im selben Zeitraum** und Alpha. Filter z. B. offen/geschlossen, ohne Sparpläne, nur Kredit. |
| Kreditfinanzierte Trades | Kredittranchen werden **aus dem Scalable-Kassenbuch rekonstruiert**, die **echten Sollzinsen** werden verursachungsgerecht verteilt. Ergebnis: Eigenkapital-IRR nach Steuern und Zinsen, Nettogewinn, effektiver Zinssatz, Break-even-Rendite (Zinsen sind nicht absetzbar), Beleihung und Stresstest. |

**Steuern:** Abgeltungsteuer + Soli (+ Kirchensteuer), Teilfreistellung je Fondsart, Vorabpauschale mit Anrechnung beim Verkauf, latente Steuer auf offene Gewinne. Die Jahresübersicht enthält Verlusttöpfe und Freistellungsauftrag und schlägt vor, wie sich der restliche Freibetrag nutzen lässt.

```bash
stopguard portfolio sync                 # Daten von Scalable laden (nur lesend; erster Lauf dauert länger)
stopguard portfolio summary              # Kennzahlen
stopguard portfolio trades --no-plans    # Trade-Vergleich (Sortierung --sort irr|profit|holding_days)
stopguard portfolio credit               # Kredit-Trades, Zinsen, Stresstest
stopguard portfolio taxes                # Steuern je Jahr, Freibetrag, latente Steuer
stopguard portfolio positions | income | instruments
stopguard portfolio export --out portfolio.json   # komplette Auswertung für eine UI
stopguard portfolio serve                # lokale JSON-API: http://127.0.0.1:8765/api/v1/summary
```

**Notion-Historie übernehmen:** In Notion die Datenbank „Trades“ exportieren (••• → Export → Markdown & CSV) und so importieren:

```bash
stopguard portfolio import Trades.csv --source notion
```

Erkannt werden die Spalten `Name Investment, Ticker, Open, Open (€), Close, Close (€), Units, Dividends (€), Current (€), Kreditfinanziert?`.

- Ticker wie `EUNL.DE` ordnest du in der Konfiguration einer ISIN zu (`[instruments."IE00B4L5Y983"] ticker = "EUNL.DE"`).
- Trades, die auch in den Scalable-Daten stehen, werden automatisch als Dubletten erkannt. Ihre Kredit-Markierung wird übernommen.
- Bei Trades anderer Broker ohne Scalable-Zinsbuchungen werden die Zinsen mit `credit.fallback_rate_pct` geschätzt.

**Daten für eigene Auswertungen:** Datenvertrag, API und Methodik stehen in [`docs/portfolio-data.md`](docs/portfolio-data.md). Ein Beispiel-Export aus der Demo liegt in [`docs/examples/portfolio-demo.json`](docs/examples/portfolio-demo.json), inklusive `chart_hints` mit Vorschlägen für Diagramme.

**Ideen für später:** Bestätigung per Telegram-Button (Vorschau aufs Handy, „Platzieren“ tippen), Positionsgrößen-/Risiko-Check pro Depot, Take-Profit-Stufen.

---

## 12. Weboberfläche

```bash
stopguard web --open        # http://127.0.0.1:8765/ – nur auf diesem Rechner erreichbar
```

Die Oberfläche nutzt das Design-System **Amboss** und funktioniert im Hell- und Dunkelmodus, vom Desktop bis zum Handy. Sie braucht keinen Build-Schritt und lädt nichts aus dem Internet, denn Schriften und Skripte liegen im Paket.

| Seite | Inhalt |
|---|---|
| **Übersicht** | Kacheln: Nettovermögen, IRR vor und nach Steuern, TWR gegen den Benchmark, Gewinn, erwartete Dividenden, Kreditauslastung. Darunter die wichtigsten Hinweise. Diagramme: Depotwert und Wertentwicklung (6 Monate bis Gesamt). Dazu Aufteilung, Stop-Schutz sowie stärkste und schwächste Trades. |
| **Positionen** | Sortierbare Tabelle mit Anteil, Kursgewinn, IRR (auch nach Steuern), Erträgen und Stop-Status, umschaltbar auf eine **Heatmap**: Fläche = Wert, Farbe = Kursentwicklung über 1 Woche bis 1 Jahr. Die Detailseite zeigt den Wertverlauf, die Phasen der Strategie und den Rechenweg des Stops. |
| **Trades** | Streudiagramm Haltedauer × IRR (ein Klick öffnet den Trade). Filter: offen/geschlossen, Herkunft (z. B. ohne Sparpläne), nur kreditfinanziert, Suche. Vergleich mit Benchmark und Alpha. Die Detailseite zeigt Zahlungsströme, Steuern und Kredit. Unter **Alle Buchungen** stehen sämtliche Kontobewegungen mit Filter, Suche und CSV-Export. |
| **Stops** | Empfohlene Änderungen, Schutz je Position, ungeklärte Einreichungen, Kursalarme, Verlauf. |
| **Auswertung** | Performance (TWR, Drawdown, IRR-Verlauf, Jahre), Hinweise, Diversifikation, Dividenden, Rebalancing, Prognose, Kredit (Saldo, Zinsen, Break-even), Steuern (Jahre, Verlusttöpfe, Freibetrag nutzen), Kosten. Abschnitt 13 erklärt die neuen Reiter. |
| **Watchlist** | Wertpapiere beobachten, die noch nicht im Depot sind. Zu sehen sind Kurs, 12-Monats-Verlauf, Veränderung, Abstand zum 52-Wochen-Hoch und zum Zielkurs. Den Zielkurs kannst du als **Kursalarm bei Scalable** hinterlegen; das ist keine Order. |
| **Daten & Einstellungen** | Daten von Scalable laden (mit Fortschritt), Datenqualität, Notion-CSV-Import, JSON-Export, Hell/Dunkel. |

Das Auge oben rechts schaltet den **Privatsphäre-Modus**: Beträge und Stückzahlen werden ausgeblendet, Prozente bleiben stehen. Das ist praktisch beim Bildschirmteilen.

Jedes Diagramm lässt sich als Tabelle anzeigen. Diagramme und Tabellen sind per Tastatur bedienbar: Pfeiltasten im Diagramm, sortierbare Spaltenköpfe.

**Orders im Browser bestätigen** läuft wie im Terminal in zwei Stufen:

1. „Vorschau von Scalable anfordern“ führt nur Phase 1 aus. Die Vorschau erscheint vollständig und unverändert, inklusive Kosten, und noch ist nichts angelegt.
2. Erst nach dem Häkchen „Ich habe alle Angaben geprüft …“ und einem Klick auf **„Order verbindlich platzieren“** läuft Phase 2. Kein Schalter erledigt beides auf einmal, und die API verlangt für diesen Schritt ausdrücklich `{"confirm": true}`.

Muss vorher eine alte Order storniert werden, fragt die Oberfläche gesondert nach. Solange die Position dann ungeschützt ist, zeigt sie eine deutliche Warnung. Unklare Einreichungen werden nie wiederholt, sondern auf der Seite „Stops“ geklärt: „Offene Order übernehmen“ oder „Keine Order angelegt“.

**Im Heimnetz, auf dem Raspberry Pi oder auf der Schmiede:**

```bash
export STOPGUARD_WEB_TOKEN=$(openssl rand -hex 24)     # Zugangsschlüssel, z. B. im Passwortmanager ablegen
stopguard web --host 0.0.0.0 --allowed-host pi.local
```

- Ohne `STOPGUARD_WEB_TOKEN` startet die Oberfläche nur auf `127.0.0.1`. Mit Token meldest du dich einmal im Browser an. Das Cookie ist HttpOnly und SameSite=Strict und gilt 30 Tage. Skripte schicken stattdessen `Authorization: Bearer <Token>`.
- Schreibende Anfragen brauchen den Kopf `X-Stopguard: 1` und dieselbe Herkunft (CSRF-Schutz). Unbekannte Host-Namen werden abgewiesen (Schutz vor DNS-Rebinding); erlaubte Namen legst du mit `--allowed-host` fest.
- HTTPS ist nicht eingebaut. Für den Zugriff von unterwegs gehört ein Reverse-Proxy mit TLS (z. B. Caddy) oder ein VPN (z. B. WireGuard oder Tailscale) davor. Meldet der Proxy `X-Forwarded-Proto: https`, wird das Cookie als `Secure` markiert.
- `GET /api/v1/health` ist ohne Anmeldung erreichbar (nur `ok` und Version) und dient als Healthcheck.
- Docker: `docker compose --profile web up -d` (Abschnitt 7).

---

## 13. Dividenden, Diversifikation und Planung

Inspiriert von Parqet und getquin; Abschnitt 14 ergänzt Funktionen nach dem Vorbild von extraETF. Alles stammt aus den eigenen Scalable-Daten und der Konfiguration. stopguard ruft keine fremden Datenquellen ab.

**Dividendenkalender und Prognose** (Auswertung → Dividenden):
- **Prognose:** Jede Ausschüttung der letzten 12 Monate wird je Stück auf deinen heutigen Bestand hochgerechnet und ein Jahr später erwartet. Hältst du eine Position noch kein Jahr, ergänzt stopguard den erkannten Rhythmus (monatlich, quartalsweise, halbjährlich).
- **Datenbasis je Position:** „volles Jahr“, „Rhythmus erkannt“ oder „eine Zahlung“.
- **Kennzahlen:** Dividendenrendite auf den heutigen Wert und auf deinen Einstand (Yield on Cost) sowie das Wachstum gegenüber dem Vorjahr.

Alle Werte sind Schätzungen, netto nach einbehaltener Steuer.

**Diversifikation und ETF-Durchschau** (Auswertung → Diversifikation):
- **Aufteilung** nach Region, Land, Branche, Anlageklasse und Währung.
- **Einzelaktien:** Land, Region und Währung leitet stopguard aus dem Länderkürzel der ISIN ab und kennzeichnet das als Näherung. Die Branche trägst du selbst ein.
- **ETFs** schlüsselt stopguard mit den Gewichten aus dem Factsheet auf.
- **Größte Einzelwerte:** Die Durchschau zeigt sie direkt und über ETFs. So fällt auf, wenn eine Aktie zusätzlich in deinen ETFs steckt.

```toml
[instruments.DE0007164600]          # SAP – Einzelaktie
sector = "Informationstechnologie"

[instruments.IE00B4L5Y983.exposure]  # iShares Core MSCI World – Werte aus dem Factsheet (Prozent)
countries  = { US = 72, JP = 5.5, GB = 3.5, CA = 3, FR = 2.7, CH = 2.4, DE = 2.2 }
sectors    = { "Informationstechnologie" = 26, "Finanzen" = 16, "Industrie" = 11, "Gesundheit" = 10 }
currencies = { USD = 72, EUR = 9, JPY = 5.5 }
holdings   = { US0378331005 = 4.8, US67066G1040 = 4.6, "Amazon" = 2.7 }
```

Fehlende Restgewichte erscheinen als „Sonstige“. Nicht zugeordnete Positionen zeigt die Seite offen an, mit dem passenden Konfigurationsschnipsel.

**Rebalancing** (Auswertung → Rebalancing): Du legst Zielgewichte je Position fest; sie werden in `<state_dir>/targets.json` gespeichert. Der Rechner verteilt einen Betrag, z. B. die nächste Sparrate, in einem von zwei Verfahren:
- **Nur kaufen:** Zuerst bekommen die Positionen Geld, die am weitesten unter ihrem Ziel liegen (Wasserstand-Verfahren). Es gibt keine Verkäufe und damit keine Steuer. Optional mit Mindestbetrag je Kauf.
- **Auf Ziel bringen:** Rechnet exakt auf die Zielgewichte, auch mit Verkäufen. Die Steuer darauf wird geschätzt.

stopguard kauft nichts; Käufe führst du selbst in der Scalable-App aus.

**Vermögensprognose** (Auswertung → Prognose):
- **Simulation:** 2.000 Verläufe (Monte Carlo) mit Startkapital, Sparrate, Zeitraum, erwarteter Rendite, Schwankung, Inflation und optionalem Ziel.
- **Vorschläge** kommen aus deiner Historie: Ø Einzahlungen, zeitgewichtete Rendite und Schwankung bisher.
- **Ergebnis:** der mittlere Verlauf, das Band, in dem 80 % der Verläufe landen, und die Wahrscheinlichkeit, das Ziel zu erreichen.

Steuern und Kosten sind nicht eingerechnet.

**Hinweise** (Übersicht und Auswertung → Hinweise): Regeln, die auf Auffälligkeiten aufmerksam machen. Jeder Hinweis nennt die Zahl dahinter und verlinkt die passende Ansicht. Es sind Beobachtungen, keine Empfehlungen:
- **Risiko:** Einzelaktie über 20 % (`portfolio.concentration_limit_pct`), Überschneidungen von Aktien mit ETFs, Region über 70 %, Branche über 40 %.
- **Performance:** Abstand zum Benchmark über 12 Monate, Rückgang vom Höchststand.
- **Kredit:** Kredit-Trades mit negativer Eigenkapital-Rendite, Kreditrahmen über 50 % genutzt, Sollzins über der Depotrendite.
- **Steuern:** ungenutzter Freibetrag zum Jahresende, offene Verluste bei gezahlter Steuer.
- **Kosten und Liquidität:** teure Kleinorders, nicht investiertes Guthaben.
- **Dividenden:** steigende erwartete Ausschüttungen.

---

## 14. Risiko, Kosten, ETF-Überschneidungen und Vorabpauschale

Nach dem Vorbild von extraETF. Auch hier kommen alle Werte aus deinen Scalable-Daten und der Konfiguration.

**Risikokennzahlen** (Auswertung → Performance):
- **Werte:** Schwankung p. a., Sharpe- und Sortino-Ratio, größter Rückgang, Beta, Korrelation und Tracking Error zum Benchmark, dazu der beste und der schwächste Monat.
- **Grundlage** ist die zeitgewichtete Rendite (TWR). Die Kennzahlen sind dadurch mit Fonds und Indizes vergleichbar, weil deine Ein- und Auszahlungen sie nicht verzerren.
- **Bei kurzer Historie** bleiben die Felder leer, statt scheinbar genaue Zahlen zu zeigen.
- **Risikofreier Zins:** `portfolio.risk_free_pct` (Standard 2 %).
- Unter „Was bedeuten die Kennzahlen?“ werden alle Begriffe erklärt.

**Monatsrenditen** (Auswertung → Performance): eine Tabelle Jahr × Monat für Depot, Benchmark oder die Differenz. Blau heißt gestiegen, Rot gefallen; das Vorzeichen steht immer dabei.

**Laufende Produktkosten** (Auswertung → Kosten):
- **Warum eine eigene Übersicht:** Die TER der Fonds steckt im Kurs. Sie taucht in keiner Buchung auf, und Scalable liefert sie nicht.
- **Eintragen:** `ter = 0.20` (Prozent p. a. laut Factsheet) je Fonds in der Konfiguration.
- **Ergebnis:** gewichtete Kostenquote des Depots (Aktien zählen mit 0 %), Kosten in € pro Jahr und der Kosteneffekt über 10, 20 und 30 Jahre.

**ETF-Überschneidungen** (Auswertung → Diversifikation):
- **Matrix:** zeigt, wie ähnlich sich deine Fonds nach Ländern und Branchen sind.
- **Beispiel:** Zwei MSCI-World-ETFs liegen bei 100 %. Ein zweiter Fonds streut dann kaum zusätzlich.
- **Hinweise:** stopguard meldet Paare ab 90 % Ähnlichkeit. Sind die Kosten unterschiedlich, steht der Kostenunterschied dabei.

**Entnahmeplan** (Auswertung → Prognose → „Entnehmen“):
- **Was simuliert wird:** wie lange ein Kapital bei einer monatlichen Entnahme reicht. Die Entnahme wächst mit der Inflation, damit die Kaufkraft gleich bleibt.
- **Ergebnis:** der Anteil der Verläufe, in denen das Geld bis zum Ende reicht, die mittlere Dauer bis zum Aufbrauchen, das Restkapital und der tatsächlich entnommene Betrag.
- **Startwert:** 4 % des Kapitals pro Jahr als Entnahme.

**Vorabpauschale-Vorschau** (Auswertung → Steuern):
- **Was:** geschätzte Vorabpauschale des laufenden Jahres für thesaurierende Fonds und die Steuer darauf. Scalable zieht sie Anfang des Folgejahres vom Verrechnungskonto ein.
- **Freibetrag:** Die Vorabpauschale gilt am ersten Werktag des neuen Jahres als zugeflossen. Deshalb rechnet stopguard mit dem Freistellungsauftrag dieses neuen Jahres.
- **Basiszins:** Eingebaut sind die Werte bis 2026 (3,20 %, BMF-Schreiben vom 13.01.2026). Spätere Jahre ergänzt du mit `tax.basiszins`.
- **Hinweis im November und Dezember:** Fällt Steuer an, zeigt stopguard dazu dein aktuelles Guthaben auf dem Konto.
