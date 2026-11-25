"""Kreditfinanzierte Trades: Tranchen, Tilgungen und Zinszuordnung.

Modus ``auto`` (Standard) – Rekonstruktion aus dem Scalable-Kassenbuch:
  * Das Verrechnungskonto wird chronologisch fortgeschrieben.
  * Rutscht der Saldo durch einen Kauf ins Minus, ist der Fehlbetrag eine Kredittranche
    dieses Trades (Wertpapierkredit/Lombard).
  * Zuflüsse tilgen: Verkaufserlöse zuerst die Tranche des verkauften Trades, sonst FIFO.
  * Einzahlungen sind ``deposit_earmark_days`` Tage für Käufe zweckgebunden (z. B. Einzahlung
    für den Sparplan): Ein Kauf in diesem Zeitraum gilt als aus der Einzahlung bezahlt –
    nicht als neuer Kredit. Nicht verwendete Beträge tilgen danach den Kredit.
  * Jede Zinsbelastung von Scalable wird nach Saldo-Tagen seit der letzten Belastung auf
    die Tranchen verteilt (also verursachungsgerecht).
  * Kurzzeitige Minussalden (≤ ``ARTIFACT_DAYS``, z. B. Wertstellungsdifferenzen) ohne Zinsen
    werden ignoriert.

Modus ``flags`` – nur markierte Trades (Notion „Kreditfinanziert?“ oder [[credit_flags]]):
  * finanziert ist der Kaufbetrag (bzw. ``amount``), getilgt wird anteilig bei Verkäufen.
  * Zinsen: echte Scalable-Zinsbuchungen (anteilig) oder – ohne Buchungen, z. B. bei Trades
    anderer Broker – monatlich geschätzt mit ``credit.fallback_rate_pct``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from stopguard.config import CreditSettings
from stopguard.portfolio.lots import LotsResult
from stopguard.portfolio.model import LedgerEvent, Lot

ARTIFACT_DAYS = 3
OVERDRAFT = "_overdraft"
EPS = 0.005


@dataclass
class CreditReport:
    mode: str
    reconstructed_loan: float | None = None
    reconstructed_cash: float | None = None
    interest_total: float = 0.0
    interest_unallocated: float = 0.0
    overdraft_draws: float = 0.0
    artifacts_removed: int = 0
    balance_series: list[tuple[datetime, float]] = field(default_factory=list)
    interest_events: list[tuple[datetime, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _years(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 86400 / 365)


def allocate_auto(events: list[LedgerEvent], lots_res: LotsResult, report: CreditReport, earmark_days: int = 5) -> None:
    by_open = {lot.open_event_id: lot for lot in lots_res.lots if lot.open_event_id}
    by_id = {lot.id: lot for lot in lots_res.lots}
    tranches: OrderedDict[str, float] = OrderedDict()
    accrued: dict[str, float] = {}
    earmarks: list[list] = []  # [verfällt_am, Betrag] – zweckgebundene Einzahlungen
    cash = 0.0
    last_t: datetime | None = None

    def lot_of(key: str) -> Lot | None:
        return by_id.get(key)

    def repay(amount: float, when: datetime, first: list[str] | None = None) -> None:
        order = list(first or [])
        order += [k for k in tranches if k not in order]
        for k in order:
            if amount <= EPS:
                break
            bal = tranches.get(k, 0.0)
            if bal <= 0:
                continue
            take = min(bal, amount)
            tranches[k] = bal - take
            amount -= take
            lot = lot_of(k)
            if lot is not None:
                lot.credit_repayments.append((when, take))
            if tranches[k] <= EPS:
                del tranches[k]

    def release_earmarks(when: datetime, force: bool = False) -> None:
        for em in list(earmarks):
            if force or em[0] <= when:
                repay(em[1], em[0] if not force else when)
                earmarks.remove(em)

    for ev in events:
        release_earmarks(ev.when)
        if last_t is not None:
            dt = (ev.when - last_t).total_seconds() / 86400
            if dt > 0:
                for k, b in tranches.items():
                    accrued[k] = accrued.get(k, 0.0) + b * dt
        last_t = ev.when

        interest_shares: dict[str, float] = {}
        if ev.kind == "loan_interest":
            amount = -ev.cash
            report.interest_total += amount
            report.interest_events.append((ev.when, amount))
            total = sum(accrued.values())
            if total > 0:
                for k, acc in accrued.items():
                    share = amount * acc / total
                    if share <= 0:
                        continue
                    interest_shares[k] = share
                    lot = lot_of(k)
                    if lot is not None:
                        lot.interest.append((ev.when, share))
                    else:
                        report.interest_unallocated += share
            else:
                report.interest_unallocated += amount
            accrued = {k: 0.0 for k in tranches}

        loan_before = max(0.0, -cash)
        cash += ev.cash
        loan_after = max(0.0, -cash)

        if loan_after > loan_before + EPS:
            draw = loan_after - loan_before
            if ev.kind == "buy" and earmarks:
                for em in list(earmarks):  # Kauf aus zweckgebundener Einzahlung
                    use = min(em[1], draw)
                    em[1] -= use
                    draw -= use
                    if em[1] <= EPS:
                        earmarks.remove(em)
                    if draw <= EPS:
                        break
            if draw <= EPS:
                pass
            elif ev.kind == "buy" and ev.id in by_open:
                key = by_open[ev.id].id
                tranches[key] = tranches.get(key, 0.0) + draw
                by_open[ev.id].credit_draws.append((ev.when, draw))
            elif ev.kind == "loan_interest" and interest_shares:
                total_share = sum(interest_shares.values())
                for k, share in interest_shares.items():  # kapitalisierte Zinsen gehören zur Tranche
                    part = draw * share / total_share
                    tranches[k] = tranches.get(k, 0.0) + part
                    lot = lot_of(k)
                    if lot is not None:
                        lot.credit_draws.append((ev.when, part))
                    else:
                        report.overdraft_draws += part
            else:
                tranches[OVERDRAFT] = tranches.get(OVERDRAFT, 0.0) + draw
                report.overdraft_draws += draw
        elif loan_after < loan_before - EPS:
            amount = loan_before - loan_after
            if ev.kind == "deposit" and earmark_days > 0:
                earmarks.append([ev.when + timedelta(days=earmark_days), amount])
            else:
                first = [lid for lid, _ in lots_res.exit_event_lot.get(ev.id, [])] if ev.kind == "sell" else []
                repay(amount, ev.when, first)
        if loan_after <= EPS and earmarks:
            release_earmarks(ev.when, force=True)  # kein Kredit mehr → Zweckbindung gegenstandslos
        report.balance_series.append((ev.when, loan_after))

    release_earmarks(events[-1].when if events else datetime.now(timezone.utc), force=True)
    report.reconstructed_cash = cash
    report.reconstructed_loan = max(0.0, -cash)

    # Wertstellungs-Artefakte entfernen
    for lot in lots_res.lots:
        if not lot.credit_draws or lot.credit_outstanding > EPS:
            continue
        first = min(d for d, _ in lot.credit_draws)
        last_repay = max((d for d, _ in lot.credit_repayments), default=first)
        if last_repay - first <= timedelta(days=ARTIFACT_DAYS):
            report.interest_unallocated += sum(a for _, a in lot.interest)
            lot.credit_draws.clear()
            lot.credit_repayments.clear()
            lot.interest.clear()
            report.artifacts_removed += 1


def allocate_flags(
    lots: list[Lot],
    loan_interest: list[LedgerEvent],
    settings: CreditSettings,
    now: datetime,
    report: CreditReport,
    *,
    use_actual_interest: bool,
) -> None:
    flagged = [lot for lot in lots if lot.flagged_credit and not lot.credit_draws]
    for lot in flagged:
        amount = lot.credit_amount if lot.credit_amount else lot.invested
        amount = min(amount, lot.invested)
        lot.credit_draws.append((lot.open_date, amount))
        for e in sorted(lot.exits, key=lambda e: e.when):
            lot.credit_repayments.append((e.when, amount * e.units / lot.units))

    def balance_at(lot: Lot, t: datetime) -> float:
        drawn = sum(a for d, a in lot.credit_draws if d <= t)
        repaid = sum(a for d, a in lot.credit_repayments if d <= t)
        return max(0.0, drawn - repaid)

    actual_targets = [lot for lot in flagged if lot.source == "scalable"] if use_actual_interest else []
    if actual_targets and loan_interest:
        prev: datetime | None = None
        for ev in sorted(loan_interest, key=lambda e: e.when):
            amount = -ev.cash
            report.interest_total += amount
            report.interest_events.append((ev.when, amount))
            start = prev or (ev.when - timedelta(days=30))
            weights = {lot.id: _balance_days(lot, start, ev.when, balance_at) for lot in actual_targets}
            total = sum(weights.values())
            if total <= 0:
                report.interest_unallocated += amount
            else:
                for lot in actual_targets:
                    if weights[lot.id] > 0:
                        lot.interest.append((ev.when, amount * weights[lot.id] / total))
            prev = ev.when

    rate = settings.fallback_rate_pct / 100
    for lot in flagged:
        if lot in actual_targets and loan_interest:
            continue
        # geschätzte Zinsen: monatlich nachschüssig auf den offenen Saldo
        end = lot.close_date or now
        t = lot.open_date
        while t < end:
            nxt = _next_month_start(t)
            period_end = min(nxt, end)
            bal = balance_at(lot, t)
            if bal > 0:
                lot.interest.append((period_end, bal * rate * _years(t, period_end)))
            t = period_end
        lot.interest_estimated = True
        report.interest_total += sum(a for _, a in lot.interest)


def _balance_days(lot: Lot, start: datetime, end: datetime, balance_at) -> float:
    points = sorted({start, end, *[d for d, _ in lot.credit_draws if start < d < end], *[d for d, _ in lot.credit_repayments if start < d < end]})
    total = 0.0
    for a, b in zip(points, points[1:], strict=False):
        total += balance_at(lot, a) * (b - a).total_seconds() / 86400
    return total


def _next_month_start(t: datetime) -> datetime:
    """Erster Tag des Folgemonats (Zinsbelastung monatlich nachschüssig)."""
    return datetime(t.year + t.month // 12, t.month % 12 + 1, 1, tzinfo=timezone.utc)


def allocate_credit(
    mode: str,
    events: list[LedgerEvent],
    lots_res: LotsResult,
    manual_lots: list[Lot],
    settings: CreditSettings,
    now: datetime,
    reported_loan: float | None,
) -> CreditReport:
    report = CreditReport(mode=mode)
    if mode == "off":
        return report
    loan_interest = [e for e in events if e.kind == "loan_interest"]
    if mode == "auto" and events:
        allocate_auto(events, lots_res, report, settings.deposit_earmark_days)
        flagged_without_credit = [lot for lot in lots_res.lots if lot.flagged_credit and not lot.credit_draws]
        for lot in flagged_without_credit:
            report.warnings.append(
                f"{lot.name} ({lot.open_date:%d.%m.%Y}) ist als kreditfinanziert markiert, im Kassenbuch aber ohne Kredit"
            )
        if reported_loan is not None and report.reconstructed_loan is not None:
            diff = abs(reported_loan - report.reconstructed_loan)
            if diff > max(5.0, 0.01 * max(reported_loan, report.reconstructed_loan)):
                report.warnings.append(
                    f"Kredit laut Scalable {reported_loan:,.2f} € ≠ rekonstruiert {report.reconstructed_loan:,.2f} € – "
                    "Historie unvollständig? Alternativ credit.mode = \"flags\" verwenden."
                )
        allocate_flags(manual_lots, [], settings, now, report, use_actual_interest=False)
    else:
        allocate_flags(lots_res.lots + manual_lots, loan_interest, settings, now, report, use_actual_interest=True)
    return report
