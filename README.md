# stopguard – dynamische Stop-Loss-Verwaltung für Scalable Capital

`stopguard` hält die Schutz-Orders in deinem Scalable-Capital-Depot aktuell, und zwar nach Regeln, die die App selbst nicht kennt:

- **Stop nach IRR:** Der Stop liegt immer dort, wo die Position noch eine Mindestrendite p.a. bringt. Mit der Zeit steigt er automatisch.
- **Nachziehen statt Veralten:** Steigt ein Papier, zieht der Stop mit, z. B. immer max. 5 % unter dem Hoch. Dafür wird, wo möglich, der native Trailing-Stop des Brokers genutzt.
- **Phasen-Logik:** z. B. „bis 20 % Verlust tolerieren, bis **erstmals** 5 % IRR erreicht sind, danach max. 5 % Verlust“.

Dazu kommen Backtests, Kursalarme, Push-Benachrichtigungen aufs Handy, ein Audit-Log und ein Betriebsmodus für den Server.

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
```

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
pytest                    # 48 Tests: XIRR (Excel-Referenz), Phasen, Planung, kompletter Order-Ablauf gegen simuliertes Backend
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

**Ideen für später:** Bestätigung per Telegram-Button (Vorschau aufs Handy, „Platzieren“ tippen), HTML-Report, Positionsgrößen-/Risiko-Check pro Depot, Take-Profit-Stufen.
