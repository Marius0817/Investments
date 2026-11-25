"""Steuermodell für Privatanleger in Deutschland (Abgeltungsteuer).

Bewusst als *Grenzsteuer-Modell je Trade* umgesetzt: Jeder Trade wird so besteuert,
als fiele die Steuer zum vollen Satz an (Freistellungsauftrag und Verlusttöpfe sind
Effekte auf Depot-Ebene und werden in der Jahresübersicht separat gezeigt). So bleiben
Trades untereinander vergleichbar.

Berücksichtigt:
- Abgeltungsteuer 25 % + Soli 5,5 % (+ Kirchensteuer 8/9 % mit Sonderausgaben-Effekt)
- Teilfreistellung je Fondsart (§ 20 InvStG)
- Vorabpauschale für thesaurierende Fonds (§ 18 InvStG), Anrechnung beim Verkauf
- Verluste mindern die Steuer (``loss_offset``), Kreditzinsen sind NICHT abzugsfähig
  (Werbungskostenabzugsverbot § 20 Abs. 9 EStG)

Keine Steuerberatung – Abweichungen zur tatsächlichen Abrechnung sind möglich
(Quellensteuer, Verlustverrechnungsreihenfolge, Freibetrag, Ersatzbemessungsgrundlage …).
"""

from __future__ import annotations

from datetime import datetime, timezone

from stopguard.config import TaxSettings
from stopguard.portfolio.model import Dividend, Instrument, Lot, LotExit
from stopguard.portfolio.prices import PriceBook

# Basiszins für die Vorabpauschale in Prozent (BMF-Schreiben). Negative Werte → keine Vorabpauschale.
BASISZINS = {2018: 0.87, 2019: 0.52, 2020: 0.07, 2021: -0.45, 2022: -0.05, 2023: 2.55, 2024: 2.29, 2025: 2.53}


def effective_rate(church_tax_pct: float = 0.0) -> float:
    """Gesamtsteuersatz auf Kapitalerträge.

    Ohne Kirchensteuer: 25 % × 1,055 = 26,375 %.
    Mit Kirchensteuer k: Abgeltungsteuer = 1 / (4 + k), darauf Soli 5,5 % und KiSt k.
    """
    if church_tax_pct:
        k = church_tax_pct / 100
        abgst = 1 / (4 + k)
        return abgst * (1 + 0.055 + k)
    return 0.25 * 1.055


class TaxModel:
    def __init__(self, settings: TaxSettings, prices: PriceBook, now: datetime):
        self.s = settings
        self.prices = prices
        self.now = now
        self.rate = effective_rate(settings.church_tax_pct) if settings.enabled else 0.0
        self.basiszins = {**BASISZINS, **settings.basiszins}
        self.notes: set[str] = set()

    # ------------------------------------------------------- Vorabpauschale
    def apply_vorabpauschale(self, lot: Lot, inst: Instrument) -> None:
        lot.vap_tax.clear()
        lot.vap_per_unit.clear()
        if not (self.s.enabled and self.s.vorabpauschale and inst.accumulating):
            return
        for year in range(lot.open_date.year, self.now.year):
            year_end = datetime(year, 12, 31, 23, 59, tzinfo=timezone.utc)
            units = lot.units_at(year_end)
            if units <= 0:
                continue
            bz = self.basiszins.get(year)
            if bz is None:
                self.notes.add(f"Basiszins {year} nicht hinterlegt – Vorabpauschale {year} ausgelassen (tax.basiszins ergänzen)")
                continue
            if bz <= 0:
                continue
            p_start = self.prices.at(lot.isin, datetime(year, 1, 1, tzinfo=timezone.utc), tolerance_days=7)
            p_end = self.prices.at(lot.isin, year_end)
            if p_start is None and lot.open_date.year == year:
                p_start = lot.price  # Kurshistorie beginnt erst mit dem Kauf
            if p_start is None or p_end is None:
                self.notes.add(f"{inst.name}: Kurse für Vorabpauschale {year} fehlen – ausgelassen")
                continue
            factor = 1.0 if lot.open_date.year < year else (13 - lot.open_date.month) / 12
            base = p_start * bz / 100 * 0.7 * factor
            vap_unit = max(0.0, min(base, p_end - p_start))
            if vap_unit <= 0:
                continue
            due = datetime(year + 1, 1, 2, 12, tzinfo=timezone.utc)
            lot.vap_per_unit.append((due, vap_unit))
            lot.vap_tax.append((due, vap_unit * units * (1 - inst.teilfreistellung) * self.rate))

    def vap_cum_per_unit(self, lot: Lot, until: datetime) -> float:
        return sum(v for d, v in lot.vap_per_unit if d <= until)

    # ---------------------------------------------------------------- Sätze
    def _tax_on(self, taxable: float) -> float:
        if not self.s.enabled:
            return 0.0
        if taxable < 0 and not self.s.loss_offset:
            return 0.0
        return taxable * self.rate

    def sale_gain(self, lot: Lot, inst: Instrument, units: float, price: float, fees: float, when: datetime) -> float:
        """Steuerpflichtiger Gewinn (vor Teilfreistellung) – Vorabpauschalen werden angerechnet."""
        cost = units * lot.price + (lot.fees * units / lot.units if lot.units else 0.0)
        return units * price - fees - cost - self.vap_cum_per_unit(lot, when) * units

    def exit_tax(self, lot: Lot, inst: Instrument, e: LotExit) -> float:
        if self.s.method == "actual" and e.tax_actual is not None:
            return e.tax_actual
        gain = self.sale_gain(lot, inst, e.units, e.price, e.fees, e.when)
        return self._tax_on(gain * (1 - inst.teilfreistellung))

    def latent_tax(self, lot: Lot, inst: Instrument, price: float) -> float:
        units = lot.remaining_units
        if units <= 0:
            return 0.0
        gain = self.sale_gain(lot, inst, units, price, 0.0, self.now)
        return self._tax_on(gain * (1 - inst.teilfreistellung))

    def dividend_tax(self, d: Dividend, inst: Instrument) -> tuple[float, float]:
        """(Zufluss brutto, Steuer). Ohne Bruttoangabe gilt der Nettobetrag als bereits versteuert."""
        if d.gross is None:
            return d.net, 0.0
        if self.s.method == "actual":
            return d.gross, max(0.0, d.gross - d.net)
        return d.gross, max(0.0, self._tax_on(d.gross * (1 - inst.teilfreistellung)))

    def interest_tax_effect(self, interest: float) -> float:
        """Steuerersparnis durch Kreditzinsen – bei Abgeltungsteuer 0 (nicht abzugsfähig)."""
        return interest * self.rate if self.s.interest_deductible else 0.0

    def break_even_irr(self, interest_rate: float, inst: Instrument) -> float:
        """Vorsteuer-Rendite, die nötig ist, um Kreditzinsen nach Steuern zu decken."""
        keep = 1 - self.rate * (1 - inst.teilfreistellung)
        if self.s.interest_deductible:
            return interest_rate
        return interest_rate / keep if keep > 0 else float("inf")
