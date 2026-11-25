"""Zweistufiger Order-Ablauf für die Weboberfläche.

Entspricht der Vorgabe der Scalable CLI (Vorschau → separate, ausdrückliche Zustimmung →
``--confirm``): ``preview`` führt nur Phase 1 aus und gibt die vollständige Vorschau zurück;
erst ein eigener Aufruf von ``confirm`` – ausgelöst durch einen Klick auf „Order verbindlich
platzieren“ – führt Phase 2 aus. Es gibt keinen Weg, beides in einem Schritt auszulösen.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from stopguard.config import Config
from stopguard.engine import Action, Engine
from stopguard.executor import RECOVERY_TEXT, Executor
from stopguard.models import utcnow
from stopguard.notify import Notifier
from stopguard.sc import OrderSpec, ScClient, ScError
from stopguard.state import StateStore

PREVIEW_KINDS = ("place", "replace", "sell_now")


class OrderFlowError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def action_key(a: Action) -> str:
    return f"{a.kind}:{a.isin}"


def action_dict(a: Action) -> dict[str, Any]:
    return {
        "key": action_key(a),
        "kind": a.kind,
        "isin": a.isin,
        "name": a.name,
        "headline": a.headline(),
        "reason": a.reason,
        "current": a.current,
        "new": a.spec.describe() if a.spec else None,
        "spec": a.spec.to_dict() if a.spec else None,
        "cancel_order_id": a.cancel_order_id,
        "alert_price": float(a.alert_price) if a.alert_price is not None else None,
        "urgent": a.urgent,
        "needs_confirmation": a.needs_confirmation,
    }


@dataclass
class PendingPreview:
    id: str
    action: Action
    spec: OrderSpec
    confirmation_id: str
    expires_at_epoch: float | None
    preview: dict[str, Any]
    cancelled_first: bool
    created: float = field(default_factory=time.time)


class OrderFlow:
    def __init__(self, cfg: Config, client: ScClient, store: StateStore, notifier: Notifier):
        self.cfg = cfg
        self.sc = client
        self.store = store
        self.notifier = notifier
        self._pending: dict[str, PendingPreview] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- helpers
    def _executor(self, state) -> Executor:
        return Executor(self.sc, self.store, state, self.notifier, out=lambda s: None, ask=lambda *a, **k: False)

    def _find_action(self, state, key: str) -> Action:
        kind, _, isin = key.partition(":")
        report = Engine(self.cfg, self.sc, self.store, state).analyse(isins={isin})
        act = next((a for a in report.actions if action_key(a) == key), None)
        if act is None:
            raise OrderFlowError(
                "action_gone",
                "Für diese Position ist keine Änderung mehr offen – die Empfehlung wurde inzwischen neu berechnet.",
                409,
            )
        if act.kind not in PREVIEW_KINDS or act.spec is None:
            raise OrderFlowError("not_an_order", "Diese Empfehlung ist keine Order.", 400)
        if state.position(isin).unresolved_submission:
            raise OrderFlowError(
                "unresolved",
                "Eine frühere Einreichung für dieses Papier ist ungeklärt. Bitte zuerst klären (Stops → Ungeklärte Einreichungen).",
                409,
            )
        return act

    def _expire(self) -> None:
        now = time.time()
        for pid, p in list(self._pending.items()):
            if p.expires_at_epoch and now > p.expires_at_epoch + 60:
                del self._pending[pid]

    # ---------------------------------------------------------------- API
    def preview(self, key: str, *, cancel_first: bool = False) -> dict[str, Any]:
        """Phase 1 – legt KEINE Order an. Bei ``cancel_first`` wird zuvor die alte Order storniert."""
        with self._lock:
            self._expire()
            with self.store.lock(wait=True):
                state = self.store.load()
                try:
                    act = self._find_action(state, key)
                    spec = act.spec
                    assert spec is not None
                    cancelled = False
                    if cancel_first and act.cancel_order_id:
                        ex = self._executor(state)
                        self.sc.trade_cancel(act.cancel_order_id)
                        self.store.audit("order_cancelled", isin=act.isin, order_id=act.cancel_order_id, reason="replace", via="web")
                        if not ex._wait_closed(act.cancel_order_id):
                            self.notifier.send(f"{act.name}: ungeschützt", "Stornierung nicht bestätigt – neue Order nicht angelegt", "urgent")
                            raise OrderFlowError(
                                "cancel_unconfirmed",
                                "Die alte Order wurde storniert, die Stornierung ist aber noch nicht bestätigt. "
                                "Bitte in der Scalable-App prüfen und die Vorschau danach erneut anfordern.",
                                502,
                            )
                        ex._forget_order(act.isin)
                        cancelled = True
                    try:
                        preview = self.sc.trade_preview(spec)
                    except ScError as exc:
                        if act.cancel_order_id and not cancelled and exc.exit_code == 10 and not exc.is_auth_problem:
                            return {
                                "status": "needs_cancel_first",
                                "action": action_dict(act),
                                "message": (
                                    "Scalable lässt die Vorschau nicht zu, solange die bestehende Order aktiv ist – "
                                    "vermutlich sind die Stücke blockiert. Du kannst die alte Order zuerst stornieren; "
                                    "die Position ist dann bis zur Bestätigung der neuen Order ungeschützt."
                                ),
                                "error": exc.friendly(),
                            }
                        raise
                finally:
                    self.store.save(state)
            confirmation = (preview or {}).get("confirmation") or {}
            cid = confirmation.get("id")
            if not cid:
                raise OrderFlowError("preview_invalid", "Die Vorschau von Scalable enthält keine Bestätigungs-ID.", 502)
            pid = uuid.uuid4().hex
            expires = confirmation.get("expires_at_epoch")
            self._pending[pid] = PendingPreview(
                pid, act, spec, str(cid), float(expires) if isinstance(expires, (int, float)) else None, preview, cancelled
            )
            return {
                "status": "preview",
                "id": pid,
                "action": action_dict(act),
                "presentation": preview.get("presentation") or {},
                "expires_at_epoch": expires,
                "cancelled_first": cancelled,
                "replaces_order_id": act.cancel_order_id,
            }

    def get(self, pid: str) -> dict[str, Any]:
        p = self._pending.get(pid)
        if p is None:
            raise OrderFlowError("preview_unknown", "Diese Vorschau ist nicht mehr vorhanden – bitte neu anfordern.", 404)
        return {
            "status": "preview",
            "id": p.id,
            "action": action_dict(p.action),
            "presentation": p.preview.get("presentation") or {},
            "expires_at_epoch": p.expires_at_epoch,
            "cancelled_first": p.cancelled_first,
            "replaces_order_id": p.action.cancel_order_id,
        }

    def confirm(self, pid: str) -> dict[str, Any]:
        """Phase 2 – nur nach ausdrücklicher Bestätigung im Browser aufrufen."""
        with self._lock:
            p = self._pending.pop(pid, None)
            if p is None:
                raise OrderFlowError("preview_unknown", "Diese Vorschau ist nicht mehr vorhanden – bitte neu anfordern.", 404)
            act = p.action
            with self.store.lock(wait=True):
                state = self.store.load()
                ex = self._executor(state)
                try:
                    if p.expires_at_epoch and time.time() > p.expires_at_epoch - 5:
                        raise OrderFlowError(
                            "expired",
                            "Die Bestätigung ist abgelaufen (Scalable erlaubt 15 Minuten). Bitte eine neue Vorschau anfordern.",
                            410,
                            unprotected=p.cancelled_first,
                        )
                    if act.cancel_order_id and not p.cancelled_first:
                        self.sc.trade_cancel(act.cancel_order_id)
                        self.store.audit("order_cancelled", isin=act.isin, order_id=act.cancel_order_id, reason="replace", via="web")
                        if not ex._wait_closed(act.cancel_order_id):
                            self.notifier.send(f"{act.name}: ungeschützt", "Stornierung nicht bestätigt – neue Order NICHT eingereicht", "urgent")
                            raise OrderFlowError(
                                "cancel_unconfirmed",
                                "Die alte Order wurde storniert, die Stornierung ist aber nicht bestätigt – die neue Order wurde "
                                "NICHT eingereicht. Bitte in der Scalable-App prüfen.",
                                502,
                                unprotected=True,
                            )
                        ex._forget_order(act.isin)
                    attempt = {
                        "at": utcnow().isoformat(),
                        "spec": p.spec.to_dict(),
                        "confirmation_id": p.confirmation_id,
                        "expires_at_epoch": p.expires_at_epoch,
                        "replaced_order_id": act.cancel_order_id,
                        "via": "web",
                    }
                    try:
                        submitted = self.sc.trade_submit(p.spec, p.confirmation_id)
                    except ScError as exc:
                        if exc.submission_outcome_unknown:
                            state.position(act.isin).unresolved_submission = {**attempt, "error": exc.friendly()}
                            self.store.audit("submission_unknown", isin=act.isin, error=exc.friendly(), **attempt)
                            self.notifier.send(f"{act.name}: Ordereinreichung unklar", "Bitte Einreichung klären (Stops).", "urgent")
                            raise OrderFlowError("outcome_unknown", RECOVERY_TEXT, 502, unprotected=bool(act.cancel_order_id)) from exc
                        self.store.audit("submission_rejected", isin=act.isin, error=exc.friendly(), **attempt)
                        raise OrderFlowError(
                            "rejected",
                            f"Scalable hat die Order abgelehnt: {exc.friendly()}",
                            422,
                            unprotected=bool(act.cancel_order_id),
                        ) from exc
                    order_id = (((submitted or {}).get("result") or {}).get("order_submission") or {}).get("order_id")
                    ps = state.position(act.isin)
                    if p.spec.order_type != "market":
                        ps.managed_order_id = str(order_id) if order_id else None
                        ps.managed_order_spec = p.spec.to_dict()
                        ps.managed_order_placed_at = utcnow().isoformat()
                    self.store.audit("order_placed", isin=act.isin, order_id=order_id, spec=p.spec.to_dict(), replaced=act.cancel_order_id, via="web")
                    return {"status": "placed", "order_id": order_id, "action": action_dict(act), "description": p.spec.describe()}
                finally:
                    self.store.save(state)

    def discard(self, pid: str) -> dict[str, Any]:
        with self._lock:
            p = self._pending.pop(pid, None)
        if p is None:
            return {"status": "discarded", "unprotected": False}
        self.store.audit("order_declined", isin=p.action.isin, spec=p.spec.to_dict(), via="web")
        if p.cancelled_first:
            self.notifier.send(f"{p.action.name}: ungeschützt", "Neue Order abgelehnt – alte Order wurde bereits storniert", "urgent")
            self.store.audit("unprotected", isin=p.action.isin, reason="neue Order im Browser verworfen", via="web")
        return {"status": "discarded", "unprotected": p.cancelled_first}

    def resolve(self, isin: str, decision: str) -> dict[str, Any]:
        """Ungeklärte Einreichung klären: ``adopt`` (offene Order übernehmen) oder ``none`` (keine Order angelegt)."""
        with self.store.lock(wait=True):
            state = self.store.load()
            ps = state.position(isin)
            info = ps.unresolved_submission
            if not info:
                return {"status": "nothing_to_resolve"}
            try:
                if decision == "adopt":
                    orders = [
                        o
                        for o in self.sc.open_orders()
                        if o.isin == isin and o.side == "SELL" and o.order_id != info.get("replaced_order_id")
                    ]
                    if not orders:
                        raise OrderFlowError("no_order_found", "Es wurde keine offene Verkaufsorder für dieses Papier gefunden.", 404)
                    ps.managed_order_id = orders[0].order_id
                    ps.managed_order_spec = info.get("spec")
                    ps.unresolved_submission = None
                    self.store.audit("unresolved_cleared", isin=isin, order_id=orders[0].order_id, via="web")
                    return {"status": "adopted", "order_id": orders[0].order_id}
                if decision == "none":
                    ps.unresolved_submission = None
                    self.store.audit("unresolved_cleared", isin=isin, order_id=None, via="web")
                    return {"status": "cleared"}
                raise OrderFlowError("bad_decision", "Unbekannte Entscheidung.", 400)
            finally:
                self.store.save(state)

    def candidates(self, isin: str) -> list[dict[str, Any]]:
        state = self.store.load()
        info = state.position(isin).unresolved_submission or {}
        return [
            {"order_id": o.order_id, "description": o.describe(), "status": o.status}
            for o in self.sc.open_orders()
            if o.isin == isin and o.side == "SELL" and o.order_id != info.get("replaced_order_id")
        ]
