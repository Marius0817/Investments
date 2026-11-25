"""Analyse aller Positionen und Ableitung des Maßnahmenplans."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from stopguard.config import Config, Phase, PositionConfig, Strategy
from stopguard.finance import PositionReturns, build_cash_flows, position_returns
from stopguard.models import (
    Holding,
    OpenOrder,
    PriceAlert,
    Quote,
    fmt_decimal,
    parse_time,
    round_down,
    utcnow,
)
from stopguard.sc import ScClient, ScError, OrderSpec
from stopguard.state import State, StateStore
from stopguard.strategy import (
    Metrics,
    StopTarget,
    advance_phase,
    build_target,
    compute_components,
)

log = logging.getLogger(__name__)

TX_RESYNC_WINDOW = timedelta(days=10)


# ---------------------------------------------------------------------------
# Ergebnis-Strukturen
# ---------------------------------------------------------------------------

@dataclass
class Action:
    kind: str  # place | replace | cancel | sell_now | alert_set | alert_remove
    isin: str
    name: str
    reason: str
    spec: OrderSpec | None = None
    cancel_order_id: str | None = None
    current: str | None = None  # Beschreibung des Ist-Zustands
    alert_price: Decimal | None = None
    alert_id: str | None = None
    urgent: bool = False

    @property
    def needs_confirmation(self) -> bool:
        return self.kind in ("place", "replace", "cancel", "sell_now")

    def signature(self) -> str:
        spec = self.spec.to_dict() if self.spec else {}
        return f"{self.kind}:{self.isin}:{spec.get('order_type')}:{spec.get('stop_price')}:{spec.get('trail_percent')}:{spec.get('shares')}"

    def headline(self) -> str:
        labels = {
            "place": "Stop-Order anlegen",
            "replace": "Order ersetzen",
            "cancel": "Order stornieren",
            "sell_now": "SOFORT VERKAUFEN (Stop-Niveau unterschritten)",
            "alert_set": "Kursalarm setzen",
            "alert_remove": "Kursalarm entfernen",
        }
        return labels.get(self.kind, self.kind)


@dataclass
class Event:
    level: str  # info | warning | urgent
    title: str
    message: str
    key: str  # für Deduplizierung von Benachrichtigungen
    isin: str | None = None


@dataclass
class PositionAnalysis:
    isin: str
    name: str
    holding: Holding
    quote: Quote | None
    price: Decimal | None
    cfg: PositionConfig
    strategy: Strategy
    phase_index: int
    phase: Phase
    phase_changed: bool
    activation_reason: str | None
    first_seen: bool
    high_water_mark: Decimal | None
    returns: PositionReturns
    target: StopTarget
    protective_orders: list[OpenOrder] = field(default_factory=list)
    managed_order: OpenOrder | None = None
    other_sell_orders: list[OpenOrder] = field(default_factory=list)
    managed_alert: PriceAlert | None = None
    breach: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def gain(self) -> float | None:
        if self.price is None or not self.holding.fifo_price:
            return None
        return float(self.price / self.holding.fifo_price - 1)

    @property
    def distance_to_stop(self) -> float | None:
        stop = self.effective_stop
        if stop is None or not self.price:
            return None
        return float(stop / self.price - 1)

    @property
    def effective_stop(self) -> Decimal | None:
        """Tatsächlich wirksamer Stop (Order) – sonst Soll-Stop."""
        if self.managed_order and self.managed_order.stop_price is not None:
            return self.managed_order.stop_price
        return self.target.stop


@dataclass
class Report:
    at: datetime
    portfolio_id: str | None
    analyses: list[PositionAnalysis] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def order_actions(self) -> list[Action]:
        return [a for a in self.actions if a.needs_confirmation]

    def alert_actions(self) -> list[Action]:
        return [a for a in self.actions if not a.needs_confirmation]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, config: Config, client: ScClient, store: StateStore, state: State, now: datetime | None = None):
        self.cfg = config
        self.sc = client
        self.store = store
        self.state = state
        self.now = now or utcnow()

    # ----------------------------------------------------------- data sync
    def sync_transactions(self) -> None:
        """Inkrementeller Abgleich der Transaktionshistorie (für die IRR)."""
        synced = parse_time(self.state.transactions_synced_at)
        from_time = None
        if synced is not None and self.state.transactions:
            from_time = (synced - TX_RESYNC_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
        txs = self.sc.transactions(from_time=from_time)
        self.state.merge_transactions(txs)
        self.state.transactions_synced_at = self.now.isoformat()

    # ------------------------------------------------------------- analyse
    def analyse(self, *, isins: set[str] | None = None, fresh_quotes: bool = True) -> Report:
        holdings, portfolio_id = self.sc.holdings()
        report = Report(at=self.now, portfolio_id=portfolio_id)
        expected = self.cfg.scalable.portfolio_id
        if expected and portfolio_id and expected != portfolio_id:
            raise RuntimeError(
                f"Aktives Portfolio {portfolio_id} entspricht nicht der Konfiguration ({expected}). "
                "Bitte 'sc broker context select --portfolio-id <ID>' ausführen."
            )

        self.sync_transactions()
        transactions = self.state.cached_transactions()
        orders = self.sc.open_orders()
        held = {h.isin: h for h in holdings}

        self._check_vanished_orders(orders, held, report)

        positions: list[tuple[Holding, PositionConfig]] = []
        for h in holdings:
            pcfg = self.cfg.position_for(h.isin)
            if pcfg is None or (isins and h.isin not in isins):
                continue
            positions.append((h, pcfg))

        alerts: list[PriceAlert] = []
        if any(p.execution == "alert" for _, p in positions):
            alerts = self.sc.price_alerts(active_only=False)

        for h, pcfg in positions:
            try:
                analysis = self._analyse_position(h, pcfg, transactions, orders, alerts, fresh_quotes)
            except ScError as exc:
                if exc.is_auth_problem or exc.is_device_locked:
                    raise
                report.events.append(
                    Event("warning", f"{h.name}: Daten nicht abrufbar", exc.friendly(), f"err:{h.isin}:{exc.code}", h.isin)
                )
                continue
            report.analyses.append(analysis)
            self._plan(analysis, report)

        # Konfigurierte, aber nicht (mehr) im Depot befindliche Positionen
        for isin, pcfg in self.cfg.positions.items():
            if isin not in held and pcfg.enabled and (not isins or isin in isins):
                if isin in self.state.positions:
                    self.store.audit("position_closed", isin=isin)
                    del self.state.positions[isin]
        return report

    def _check_vanished_orders(self, orders: list[OpenOrder], held: dict[str, Holding], report: Report) -> None:
        open_ids = {o.order_id for o in orders}
        for isin, ps in list(self.state.positions.items()):
            oid = ps.managed_order_id
            if not oid or oid in open_ids:
                continue
            status = "UNBEKANNT"
            try:
                details = self.sc.transaction_details(oid)
                status = str((details.get("security_trade") or {}).get("status") or details.get("status") or status).upper()
            except ScError as exc:
                if exc.is_auth_problem or exc.is_device_locked:
                    raise
            name = held[isin].name if isin in held else isin
            if status in ("FILLED", "SETTLED", "CONFIRMED", "PARTIAL_FILLED"):
                report.events.append(
                    Event("urgent", f"{name}: Stop-Order ausgeführt", f"Die verwaltete Order {oid} wurde ausgeführt ({status}).", f"filled:{oid}", isin)
                )
                self.store.audit("order_filled", isin=isin, order_id=oid, status=status)
            else:
                report.events.append(
                    Event(
                        "warning",
                        f"{name}: Stop-Order nicht mehr aktiv",
                        f"Order {oid} ist nicht mehr offen (Status {status}). Die Position ist ggf. ungeschützt.",
                        f"vanished:{oid}",
                        isin,
                    )
                )
                self.store.audit("order_vanished", isin=isin, order_id=oid, status=status)
            ps.managed_order_id = None
            ps.managed_order_spec = None

    def _analyse_position(
        self,
        h: Holding,
        pcfg: PositionConfig,
        transactions,
        orders: list[OpenOrder],
        alerts: list[PriceAlert],
        fresh_quotes: bool,
    ) -> PositionAnalysis:
        en = self.cfg.engine
        strategy = self.cfg.strategy(pcfg.strategy)
        ps = self.state.position(h.isin)
        issues: list[str] = []

        quote: Quote | None = None
        if fresh_quotes and (pcfg.explicit or pcfg.execution != "watch"):
            quote = self.sc.quote(h.isin)
        price = quote.price(en.price_basis) if quote else h.mid_price
        outdated = bool(quote.outdated) if quote else bool(h.quote_outdated)
        if price is None:
            issues.append("Kein aktueller Kurs verfügbar")

        flows, quality, notes = build_cash_flows(
            h.isin, transactions, h, entry_date=pcfg.entry_date, entry_price=pcfg.entry_price
        )
        returns = position_returns(
            flows,
            float(h.quantity),
            float(price) if price is not None else None,
            self.now,
            quality=quality,
            notes=notes,
            exit_costs=en.exit_costs_eur,
        )
        issues.extend(returns.notes)
        cost_price = float(pcfg.entry_price or h.fifo_price) if (pcfg.entry_price or h.fifo_price) else (
            returns.invested / returns.shares if returns.shares else None
        )

        metrics = Metrics(
            price=float(price) if price is not None else None,
            cost_price=cost_price,
            irr=returns.irr,
            total_return=returns.total_return,
            holding_days=returns.holding_days,
        )

        # --- Strategie gewechselt? → Phasen, Hochpunkt und Ratchet neu beginnen ---
        if ps.strategy != strategy.name:
            if ps.strategy is not None:
                self.store.audit("strategy_changed", isin=h.isin, old=ps.strategy, new=strategy.name)
            ps.strategy = strategy.name
            ps.phase_index, ps.phase_name, ps.last_target_stop = 0, None, None

        # --- Phase bestimmen (sticky) -------------------------------------
        prev_index = min(ps.phase_index, len(strategy.phases) - 1)
        if ps.phase_name and ps.phase_name != strategy.phases[prev_index].name:
            prev_index = 0  # Phasen der Strategie wurden umbenannt/umgebaut → neu bewerten
        first_seen = ps.phase_name is None
        decision = advance_phase(strategy, prev_index, metrics) if price is not None else None
        phase_index = decision.index if decision else prev_index
        phase = strategy.phases[phase_index]
        phase_changed = bool(decision and phase_index != prev_index)
        if phase_changed or ps.phase_name != phase.name:
            ps.phase_index = phase_index
            ps.phase_name = phase.name
            ps.phase_since = self.now.isoformat()
            ps.activation_reason = decision.reason if decision else None
            ps.high_water_mark = fmt_decimal(price) if price is not None else None
            ps.high_water_at = self.now.isoformat()
            if phase_changed:
                self.store.audit(
                    "phase_changed",
                    isin=h.isin,
                    strategy=strategy.name,
                    phase=phase.name,
                    reason=decision.reason if decision else None,
                )

        # --- Hochpunkt seit Phasenbeginn --------------------------------------
        hwm = Decimal(ps.high_water_mark) if ps.high_water_mark else None
        if price is not None and not outdated and (hwm is None or price > hwm):
            hwm = price
            ps.high_water_mark = fmt_decimal(price)
            ps.high_water_at = self.now.isoformat()

        # --- Soll-Stop -------------------------------------------------------
        venue = pcfg.venue or self.cfg.scalable.venue
        components = compute_components(
            phase,
            cost_price=cost_price,
            high_water_mark=float(hwm) if hwm is not None else None,
            flows=returns.flows,
            shares=float(h.quantity),
            at=self.now,
            exit_costs=en.exit_costs_eur,
        )
        # Ratchet: der Soll-Stop sinkt nie – auch nicht über Phasenwechsel hinweg
        previous = Decimal(ps.last_target_stop) if ps.last_target_stop else None
        target = build_target(
            phase,
            components,
            price=float(price) if price is not None else None,
            venue=venue,
            decimals=en.price_decimals,
            min_distance_pct=en.min_distance_pct,
            previous_target=previous if en.only_raise else None,
            only_raise=en.only_raise,
        )
        if target.stop is not None and (ps.last_target_stop is None or target.stop > Decimal(ps.last_target_stop)):
            ps.last_target_stop = fmt_decimal(target.stop)
        issues.extend(target.notes)

        # --- Ist-Zustand: Orders & Alarme ----------------------------------
        mine = [o for o in orders if o.isin == h.isin and o.side == "SELL"]
        protective = [o for o in mine if o.is_protective_sell]
        others = [o for o in mine if not o.is_protective_sell]
        managed = next((o for o in protective if o.order_id == ps.managed_order_id), None)
        if managed is None and en.adopt_existing_orders and len(protective) == 1:
            managed = protective[0]
            if ps.managed_order_id != managed.order_id:
                ps.managed_order_id = managed.order_id
                self.store.audit("order_adopted", isin=h.isin, order_id=managed.order_id, order=managed.describe())

        managed_alert = None
        if pcfg.execution == "alert":
            mine_alerts = [a for a in alerts if a.isin == h.isin and a.is_active]
            managed_alert = next((a for a in mine_alerts if a.alert_id == ps.managed_alert_id), None)

        breach = False
        if price is not None and not outdated:
            if target.order_type == "trailing-stop":
                virtual = hwm * (1 - target.trail_percent / 100) if hwm is not None and target.trail_percent else None
                breach = managed is None and virtual is not None and price <= virtual
            elif target.stop is not None:
                breach = price <= target.stop

        return PositionAnalysis(
            isin=h.isin,
            name=pcfg.label or h.name,
            holding=h,
            quote=quote,
            price=price,
            cfg=pcfg,
            strategy=strategy,
            phase_index=phase_index,
            phase=phase,
            phase_changed=phase_changed,
            activation_reason=ps.activation_reason,
            first_seen=first_seen,
            high_water_mark=hwm,
            returns=returns,
            target=target,
            protective_orders=protective,
            managed_order=managed,
            other_sell_orders=others,
            managed_alert=managed_alert,
            breach=breach,
            issues=issues,
        )

    # --------------------------------------------------------------- plan
    def desired_spec(self, a: PositionAnalysis) -> OrderSpec | None:
        t = a.target
        if t.stop is None or t.order_type is None:
            return None
        qty = a.holding.quantity
        if a.cfg.protect_pct < 100:
            raw = qty * Decimal(str(a.cfg.protect_pct)) / 100
            qty = round_down(raw, 0) if qty == qty.to_integral_value() else round_down(raw, 6)
        return OrderSpec(
            isin=a.isin,
            shares=qty,
            order_type=t.order_type,
            stop_price=t.stop if t.order_type in ("stop", "stop-limit") else None,
            limit_price=t.limit_price if t.order_type == "stop-limit" else None,
            trail_percent=t.trail_percent if t.order_type == "trailing-stop" else None,
            venue=a.cfg.venue or self.cfg.scalable.venue,
        )

    def _plan(self, a: PositionAnalysis, report: Report) -> None:
        en = self.cfg.engine
        ps = self.state.position(a.isin)

        if a.phase_changed and not a.first_seen and a.cfg.explicit:
            report.events.append(
                Event(
                    "info",
                    f"{a.name}: neue Phase „{a.phase.name}“",
                    f"Auslöser: {a.activation_reason or '–'} · neue Regel: {a.phase.describe()}",
                    f"phase:{a.isin}:{a.phase_index}",
                    a.isin,
                )
            )

        if a.breach:
            stop_txt = fmt_decimal(a.target.stop) if a.target.stop is not None else "Trailing"
            report.events.append(
                Event(
                    "urgent",
                    f"{a.name}: Stop-Niveau erreicht",
                    f"Kurs {fmt_decimal(a.price or Decimal(0))} ≤ Soll-Stop {stop_txt} "
                    f"(Phase „{a.phase.name}“). Verkauf prüfen: stopguard apply",
                    f"breach:{a.isin}:{stop_txt}",
                    a.isin,
                )
            )

        if a.cfg.execution == "watch":
            return
        if a.cfg.execution == "alert":
            self._plan_alert(a, report)
            return

        # ---------------- execution == "order" ----------------
        if ps.unresolved_submission:
            a.issues.append("Ungeklärte Ordereinreichung – bitte 'stopguard resolve' ausführen")
            report.events.append(
                Event(
                    "warning",
                    f"{a.name}: Ordereinreichung ungeklärt",
                    "Eine frühere Order-Übermittlung hatte ein unklares Ergebnis. Bitte 'stopguard resolve' ausführen.",
                    f"unresolved:{a.isin}",
                    a.isin,
                )
            )
            return
        if len(a.protective_orders) > 1:
            a.issues.append("Mehrere Stop-Orders für dieses Papier – bitte in der App bereinigen")
            return
        if a.other_sell_orders:
            a.issues.append(
                "Weitere Verkaufsorder aktiv ("
                + ", ".join(o.describe() for o in a.other_sell_orders)
                + ") – Stückzahl ggf. blockiert, keine automatische Planung"
            )
            return
        if a.managed_order is None and a.protective_orders and not en.adopt_existing_orders:
            a.issues.append("Fremde Stop-Order vorhanden (adopt_existing_orders = false) – wird nicht verändert")
            return

        current = a.managed_order
        spec = self.desired_spec(a)

        if a.breach and spec is not None:
            if en.on_breach == "propose_sell":
                report.actions.append(
                    Action(
                        "sell_now",
                        a.isin,
                        a.name,
                        f"Kurs {fmt_decimal(a.price or Decimal(0))} liegt auf/unter dem Soll-Stop {fmt_decimal(a.target.stop)} "
                        "– eine Stop-Order kann nicht mehr platziert werden.",
                        spec=OrderSpec(a.isin, spec.shares, "market", venue=spec.venue),
                        cancel_order_id=current.order_id if current else None,
                        current=current.describe() if current else None,
                        urgent=True,
                    )
                )
            return

        if spec is None:
            if current is not None:
                a.issues.append("Aktuelle Phase sieht keinen Stop vor – bestehende Order bleibt unverändert")
            return

        if current is None:
            report.actions.append(
                Action("place", a.isin, a.name, f"Keine Schutz-Order vorhanden. Regel: {a.phase.describe()}", spec=spec)
            )
            return

        reasons: list[str] = []
        if current.kind != spec.order_type:
            reasons.append(f"Ordertyp {current.kind} → {spec.order_type} (Phase „{a.phase.name}“)")
        elif spec.order_type == "trailing-stop":
            cur_pct = current.trail_offset if current.trail_type == "PERCENTAGE" else None
            if cur_pct is None or abs(cur_pct - (spec.trail_percent or 0)) > Decimal("0.01"):
                reasons.append(f"Trailing-Abstand {fmt_decimal(cur_pct) if cur_pct is not None else '?'} → {fmt_decimal(spec.trail_percent or 0)}%")
        else:
            cur_stop = current.stop_price
            if cur_stop is None:
                reasons.append("Stop-Kurs der bestehenden Order unbekannt")
            elif spec.stop_price is not None and spec.stop_price > cur_stop:
                change = float(spec.stop_price / cur_stop - 1) * 100
                if change >= en.min_change_pct:
                    reasons.append(f"Stop nachziehen {fmt_decimal(cur_stop)} → {fmt_decimal(spec.stop_price)} (+{change:.1f}%)")
            elif spec.stop_price is not None and spec.stop_price < cur_stop and not en.only_raise:
                change = float(1 - spec.stop_price / cur_stop) * 100
                if change >= en.min_change_pct:
                    reasons.append(f"Stop senken {fmt_decimal(cur_stop)} → {fmt_decimal(spec.stop_price)} (−{change:.1f}%)")

        if current.quantity is not None and abs(current.quantity - spec.shares) > Decimal("0.000001"):
            reasons.append(f"Stückzahl {fmt_decimal(current.quantity)} → {fmt_decimal(spec.shares)} (Bestand geändert)")

        if current.valid_until is not None and current.valid_until - self.now <= timedelta(days=en.renew_before_days):
            reasons.append(f"Order läuft am {current.valid_until:%d.%m.%Y} ab – erneuern")

        if reasons:
            report.actions.append(
                Action(
                    "replace",
                    a.isin,
                    a.name,
                    "; ".join(reasons),
                    spec=spec,
                    cancel_order_id=current.order_id,
                    current=current.describe(),
                )
            )

    def _plan_alert(self, a: PositionAnalysis, report: Report) -> None:
        en = self.cfg.engine
        desired = a.target.stop
        if a.target.order_type == "trailing-stop" and a.high_water_mark is not None and a.target.trail_percent:
            desired = round_down(a.high_water_mark * (1 - a.target.trail_percent / 100), en.price_decimals)
        cur = a.managed_alert
        if desired is None:
            if cur is not None:
                report.actions.append(Action("alert_remove", a.isin, a.name, "Phase ohne Stop", alert_id=cur.alert_id))
            return
        if a.price is not None and desired >= a.price:
            return  # Alarm unter/auf aktuellem Kurs ergibt keinen Sinn – Breach-Ereignis wurde gemeldet
        if cur is None:
            report.actions.append(
                Action("alert_set", a.isin, a.name, f"Kursalarm bei Soll-Stop {fmt_decimal(desired)}", alert_price=desired)
            )
            return
        change = abs(float(desired / cur.price - 1)) * 100 if cur.price else 100.0
        if change >= en.min_change_pct and (desired > cur.price or not en.only_raise):
            report.actions.append(
                Action(
                    "alert_set",
                    a.isin,
                    a.name,
                    f"Kursalarm verschieben {fmt_decimal(cur.price)} → {fmt_decimal(desired)}",
                    alert_price=desired,
                    alert_id=cur.alert_id,
                    current=f"Alarm {fmt_decimal(cur.price)}",
                )
            )


def apply_alert_actions(client: ScClient, state: State, store: StateStore, actions: list[Action]) -> list[str]:
    """Kursalarme sind keine Orders → dürfen automatisch gepflegt werden."""
    log_lines: list[str] = []
    for act in actions:
        ps = state.position(act.isin)
        try:
            if act.kind == "alert_remove" and act.alert_id:
                client.remove_price_alert(act.alert_id)
                ps.managed_alert_id = None
                ps.managed_alert_price = None
                log_lines.append(f"{act.name}: Kursalarm entfernt")
            elif act.kind == "alert_set" and act.alert_price is not None:
                if act.alert_id:
                    client.remove_price_alert(act.alert_id)
                before = {a.alert_id for a in client.price_alerts(active_only=False) if a.isin == act.isin}
                after = client.add_price_alert(act.isin, act.alert_price)
                new = [a for a in after if a.alert_id not in before]
                match = new[0] if new else next((a for a in after if a.price == act.alert_price), None)
                ps.managed_alert_id = match.alert_id if match else None
                ps.managed_alert_price = fmt_decimal(act.alert_price)
                log_lines.append(f"{act.name}: Kursalarm bei {fmt_decimal(act.alert_price)} gesetzt")
            store.audit(act.kind, isin=act.isin, price=act.alert_price, alert_id=ps.managed_alert_id)
        except ScError as exc:
            if exc.is_auth_problem or exc.is_device_locked:
                raise
            log_lines.append(f"{act.name}: Kursalarm fehlgeschlagen – {exc.friendly()}")
    return log_lines
