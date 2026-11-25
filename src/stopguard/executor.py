"""Ausführen von Order-Änderungen – immer mit ausdrücklicher Bestätigung.

Die offizielle Scalable CLI verlangt für jede Order zwei Schritte
(public-docs/commands.md → „Trading“):

1. Vorschau (Phase 1) mit allen vorvertraglichen Informationen/Kosten,
2. eine *separate, ausdrückliche* Zustimmung des Kunden,
3. erst dann Phase 2 mit ``--confirm <ID>`` (gültig 15 Minuten).

Die Phase-1-Antwort enthält dazu u. a. ``forbid_automatic_phase_2_execution: true``.
Deshalb gibt es in stopguard bewusst KEINEN Schalter, der Phase 2 ohne Rückfrage
auslöst. Automatisch erledigt werden nur Lese-Vorgänge und Kursalarme.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from stopguard import ui
from stopguard.engine import Action
from stopguard.models import utcnow
from stopguard.notify import Notifier
from stopguard.sc import OPEN_ORDER_STATUSES, OrderSpec, ScClient, ScError
from stopguard.state import State, StateStore

RECOVERY_TEXT = (
    "Das Ergebnis der Übermittlung ist UNKLAR. Laut Scalable-Dokumentation gilt jetzt:\n"
    "  • Prüfe in der Scalable-App bzw. mit 'stopguard resolve', ob die Order angelegt wurde.\n"
    "  • Existiert sie, NICHT erneut einreichen.\n"
    "  • Lässt sich das Ergebnis nicht klären, KEINE neue Vorschau/Order erstellen.\n"
    "Bis zur Klärung plant stopguard für dieses Papier keine weiteren Orders."
)


@dataclass
class ApplyResult:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)


class Executor:
    def __init__(
        self,
        client: ScClient,
        store: StateStore,
        state: State,
        notifier: Notifier,
        *,
        out: Callable[[str], None] = print,
        ask: Callable[..., bool] = ui.ask_yes,
        sleep: Callable[[float], None] = time.sleep,
        cancel_wait_seconds: int = 60,
    ):
        self.sc = client
        self.store = store
        self.state = state
        self.notifier = notifier
        self.out = out
        self.ask = ask
        self.sleep = sleep
        self.cancel_wait_seconds = cancel_wait_seconds

    # ----------------------------------------------------------------- API
    def apply(self, actions: list[Action]) -> ApplyResult:
        result = ApplyResult()
        todo = [a for a in actions if a.needs_confirmation]
        todo.sort(key=lambda a: (not a.urgent, a.name))
        for i, act in enumerate(todo, start=1):
            self.out("")
            self.out(ui.c(f"[{i}/{len(todo)}] {act.name} ({act.isin}) – {act.headline()}", "bold", "red" if act.urgent else "cyan"))
            self.out(f"  Grund:  {act.reason}")
            if act.current:
                self.out(f"  Ist:    {act.current}")
            if act.spec:
                self.out(f"  Neu:    {act.spec.describe()}" + (f" · Handelsplatz {act.spec.venue}" if act.spec.venue else ""))
            try:
                if act.kind == "cancel":
                    self._cancel_only(act, result)
                else:
                    self._place(act, result)
            except ScError as exc:
                if exc.is_auth_problem:
                    self.out(ui.c(f"Anmeldung bei Scalable erforderlich: {exc.friendly()}", "red"))
                    self.out("Bitte 'sc login' ausführen (Login im Browser selbst abschließen).")
                    result.failed.append(f"{act.name}: Login erforderlich")
                    break
                self.out(ui.c(f"  Fehler: {exc.friendly()}", "red"))
                result.failed.append(f"{act.name}: {exc.code}")
        return result

    # -------------------------------------------------------------- steps
    def _cancel_only(self, act: Action, result: ApplyResult) -> None:
        if not self.ask("  Order jetzt stornieren?"):
            result.skipped.append(act.name)
            return
        assert act.cancel_order_id
        self.sc.trade_cancel(act.cancel_order_id)
        self._wait_closed(act.cancel_order_id)
        ps = self.state.position(act.isin)
        ps.managed_order_id = None
        ps.managed_order_spec = None
        self.store.audit("order_cancelled", isin=act.isin, order_id=act.cancel_order_id)
        result.done.append(f"{act.name}: Order storniert")

    def _place(self, act: Action, result: ApplyResult) -> None:
        assert act.spec is not None
        spec = act.spec
        cancelled_first = False

        # Schritt 1 – Vorschau (Phase 1). Bei bestehender Order zuerst MIT aktiver Order
        # versuchen, damit die Position bis zur Zustimmung geschützt bleibt.
        try:
            preview = self.sc.trade_preview(spec)
        except ScError as exc:
            if not act.cancel_order_id or exc.is_auth_problem or exc.exit_code != 10:
                raise
            self.out(ui.c(f"  Vorschau mit aktiver Order nicht möglich: {exc.friendly()}", "yellow"))
            self.out("  Wahrscheinlich sind die Stücke durch die bestehende Order blockiert.")
            if not self.ask(
                "  Bestehende Order zuerst stornieren? (Die Position ist dann bis zur Bestätigung der neuen Order ungeschützt)"
            ):
                result.skipped.append(act.name)
                return
            self.sc.trade_cancel(act.cancel_order_id)
            self.store.audit("order_cancelled", isin=act.isin, order_id=act.cancel_order_id, reason="replace")
            cancelled_first = True
            if not self._wait_closed(act.cancel_order_id):
                self._unprotected(act, result, "Stornierung nicht bestätigt – neue Order nicht angelegt")
                return
            self._forget_order(act.isin)
            preview = self.sc.trade_preview(spec)

        confirmation = (preview or {}).get("confirmation") or {}
        confirmation_id = confirmation.get("id")
        if not confirmation_id:
            raise ScError("trade_response_invalid", "Vorschau enthält keine Bestätigungs-ID", 30, "trade preview")

        # Schritt 2 – alle Informationen der Vorschau unverändert anzeigen
        self.out("")
        self.out(render_presentation(preview))
        if act.cancel_order_id and not cancelled_first:
            self.out(ui.c(f"  Hinweis: Nach deiner Bestätigung wird zuerst die bestehende Order {act.cancel_order_id} storniert.", "yellow"))

        # Schritt 3 – separate, ausdrückliche Zustimmung
        if not self.ask("  Order verbindlich bei Scalable platzieren?", strict=True):
            self.store.audit("order_declined", isin=act.isin, spec=spec.to_dict())
            if cancelled_first:
                self._unprotected(act, result, "neue Order abgelehnt – alte Order wurde bereits storniert")
            else:
                result.skipped.append(act.name)
            return

        if act.cancel_order_id and not cancelled_first:
            self.sc.trade_cancel(act.cancel_order_id)
            self.store.audit("order_cancelled", isin=act.isin, order_id=act.cancel_order_id, reason="replace")
            if not self._wait_closed(act.cancel_order_id):
                self._unprotected(act, result, "Stornierung nicht bestätigt – neue Order NICHT eingereicht, bitte App prüfen")
                return
            self._forget_order(act.isin)

        expires = confirmation.get("expires_at_epoch")
        if isinstance(expires, (int, float)) and time.time() > float(expires) - 5:
            self._unprotected(act, result, "Bestätigungs-ID abgelaufen (15 Min.) – bitte 'stopguard apply' erneut ausführen")
            return

        # Schritt 4 – Phase 2
        attempt = {
            "at": utcnow().isoformat(),
            "spec": spec.to_dict(),
            "confirmation_id": confirmation_id,
            "expires_at_epoch": expires,
            "replaced_order_id": act.cancel_order_id,
        }
        try:
            submitted = self.sc.trade_submit(spec, confirmation_id)
        except ScError as exc:
            if exc.submission_outcome_unknown:
                self.state.position(act.isin).unresolved_submission = {**attempt, "error": exc.friendly()}
                self.store.audit("submission_unknown", isin=act.isin, error=exc.friendly(), **attempt)
                self.out(ui.c("  " + RECOVERY_TEXT.replace("\n", "\n  "), "red"))
                self.notifier.send(f"{act.name}: Ordereinreichung unklar", "Bitte 'stopguard resolve' ausführen.", "urgent")
                result.failed.append(f"{act.name}: Ergebnis unklar")
                return
            self.store.audit("submission_rejected", isin=act.isin, error=exc.friendly(), **attempt)
            self.out(ui.c(f"  Order abgelehnt: {exc.friendly()}", "red"))
            if act.cancel_order_id:
                self._unprotected(act, result, "neue Order abgelehnt – alte Order ist storniert")
            else:
                result.failed.append(f"{act.name}: abgelehnt")
            return

        order_id = (((submitted or {}).get("result") or {}).get("order_submission") or {}).get("order_id")
        ps = self.state.position(act.isin)
        if spec.order_type != "market":
            ps.managed_order_id = str(order_id) if order_id else None
            ps.managed_order_spec = spec.to_dict()
            ps.managed_order_placed_at = utcnow().isoformat()
        self.store.audit("order_placed", isin=act.isin, order_id=order_id, spec=spec.to_dict(), replaced=act.cancel_order_id)
        self.out(ui.c(f"  ✔ Order platziert (ID {order_id})", "green"))
        result.done.append(f"{act.name}: {spec.describe()}")

    # ------------------------------------------------------------ helpers
    def _wait_closed(self, order_id: str) -> bool:
        """Wartet, bis die stornierte Order nicht mehr offen ist (Stücke wieder frei)."""
        deadline = time.monotonic() + self.cancel_wait_seconds
        while True:
            try:
                details = self.sc.transaction_details(order_id)
                status = str((details.get("security_trade") or {}).get("status") or details.get("status") or "").upper()
                if status and status not in OPEN_ORDER_STATUSES:
                    return True
            except ScError as exc:
                if exc.code == "broker_transaction_not_found":
                    return True
                if exc.is_auth_problem:
                    raise
            if time.monotonic() >= deadline:
                return False
            self.sleep(2)

    def _forget_order(self, isin: str) -> None:
        ps = self.state.position(isin)
        ps.managed_order_id = None
        ps.managed_order_spec = None

    def _unprotected(self, act: Action, result: ApplyResult, why: str) -> None:
        msg = f"{act.name}: POSITION UNGESCHÜTZT – {why}"
        self.out(ui.c("  ⚠ " + msg, "red", "bold"))
        self.notifier.send(f"{act.name}: ungeschützt", why, "urgent")
        self.store.audit("unprotected", isin=act.isin, reason=why)
        result.unprotected.append(msg)


def _value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def render_presentation(preview: dict[str, Any]) -> str:
    """Gibt die vorvertraglichen Informationen der Phase-1-Vorschau vollständig und
    unverändert aus (Vorgabe ``preserve_exact_values`` / ``display_null_as_literal``)."""
    pres = (preview or {}).get("presentation") or {}
    sections = pres.get("sections") or {}
    order = pres.get("section_order") or list(sections)
    lines = [ui.c("  ┌─ Vorschau von Scalable Capital (Phase 1, noch keine Order) ─", "bold")]
    if not sections:
        lines.append("  │ (keine strukturierte Darstellung – vollständige Rohdaten:)")
        for raw_line in json.dumps(preview.get("result"), indent=2, ensure_ascii=False).splitlines():
            lines.append("  │ " + raw_line)
    for key in order:
        sec = sections.get(key) or {}
        lines.append("  │")
        lines.append("  │ " + ui.c(str(sec.get("title") or key), "bold"))
        for f in sec.get("fields") or []:
            lines.append(f"  │   {f.get('label')}: {_value(f.get('value'))}")
    lines.append("  └" + "─" * 60)
    return "\n".join(lines)


def resolve_unresolved(
    client: ScClient,
    store: StateStore,
    state: State,
    *,
    out: Callable[[str], None] = print,
    ask: Callable[..., bool] = ui.ask_yes,
) -> int:
    """Geführte Klärung unklarer Einreichungen gemäß Scalable-Recovery-Prozedur."""
    pending = {isin: ps for isin, ps in state.positions.items() if ps.unresolved_submission}
    if not pending:
        out("Keine ungeklärten Ordereinreichungen.")
        return 0
    open_orders = client.open_orders()
    for isin, ps in pending.items():
        info = ps.unresolved_submission or {}
        spec_d = info.get("spec") or {}
        out("")
        out(ui.c(f"{isin}: Einreichung vom {info.get('at')}", "bold"))
        out(f"  Order:  {spec_d}")
        out(f"  Fehler: {info.get('error')}")
        candidates = [
            o
            for o in open_orders
            if o.isin == isin and o.side == "SELL" and o.order_id != info.get("replaced_order_id")
        ]
        if candidates:
            o = candidates[0]
            out(ui.c(f"  ✔ Offene Order gefunden: {o.describe()} (ID {o.order_id}) – sie wird übernommen.", "green"))
            ps.managed_order_id = o.order_id
            ps.managed_order_spec = spec_d
            ps.unresolved_submission = None
            store.audit("unresolved_cleared", isin=isin, order_id=o.order_id)
            continue
        out("  Keine offene Verkaufsorder gefunden. Bitte zusätzlich in der Scalable-App (Aufträge) prüfen.")
        if not ask("  Hast du in der App bestätigt, dass KEINE Order angelegt wurde?", strict=True):
            out("  Unverändert – bitte später erneut prüfen.")
            continue
        expires = info.get("expires_at_epoch")
        if isinstance(expires, (int, float)) and time.time() < float(expires) - 5:
            if ask("  Bestätigungs-ID ist noch gültig. Dieselbe Order mit derselben ID erneut einreichen (Duplikatschutz bleibt erhalten)?", strict=True):
                spec = OrderSpec(
                    isin=isin,
                    shares=Decimal(spec_d["shares"]),
                    order_type=spec_d["order_type"],
                    stop_price=Decimal(spec_d["stop_price"]) if spec_d.get("stop_price") else None,
                    limit_price=Decimal(spec_d["limit_price"]) if spec_d.get("limit_price") else None,
                    trail_percent=Decimal(spec_d["trail_percent"]) if spec_d.get("trail_percent") else None,
                    venue=spec_d.get("venue"),
                )
                try:
                    res = client.trade_submit(spec, str(info["confirmation_id"]))
                except ScError as exc:
                    out(ui.c(f"  Erneut fehlgeschlagen: {exc.friendly()} – Status bleibt ungeklärt.", "red"))
                    continue
                oid = (((res or {}).get("result") or {}).get("order_submission") or {}).get("order_id")
                ps.managed_order_id = str(oid) if oid else None
                ps.managed_order_spec = spec_d
                store.audit("order_placed", isin=isin, order_id=oid, spec=spec_d, retry=True)
                out(ui.c(f"  ✔ Order platziert (ID {oid})", "green"))
        ps.unresolved_submission = None
        store.audit("unresolved_cleared", isin=isin, order_id=None)
        out("  Geklärt. Beim nächsten 'stopguard apply' wird ggf. eine neue Vorschau erstellt.")
    return 0

