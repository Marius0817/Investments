"""Persistenter Zustand (JSON) + Audit-Log (JSONL) + Prozess-Lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from stopguard.models import Transaction, parse_time, utcnow

STATE_VERSION = 1


class LockBusy(RuntimeError):
    """Ein anderer stopguard-Prozess hält gerade die Sperre."""


@dataclass
class PositionState:
    strategy: str | None = None
    phase_index: int = 0
    phase_name: str | None = None
    phase_since: str | None = None
    activation_reason: str | None = None
    high_water_mark: str | None = None  # Hochpunkt seit Beginn der aktuellen Phase
    high_water_at: str | None = None
    last_target_stop: str | None = None  # höchster je berechneter Soll-Stop (Ratchet)
    managed_order_id: str | None = None
    managed_order_spec: dict[str, Any] | None = None
    managed_order_placed_at: str | None = None
    managed_alert_id: str | None = None
    managed_alert_price: str | None = None
    unresolved_submission: dict[str, Any] | None = None
    notified: dict[str, str] = field(default_factory=dict)  # Schlüssel -> Zeitpunkt


@dataclass
class State:
    positions: dict[str, PositionState] = field(default_factory=dict)
    transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    transactions_synced_at: str | None = None
    login_at: str | None = None
    last_run_at: str | None = None
    last_run_ok: bool | None = None
    last_keepalive_at: str | None = None
    notified: dict[str, str] = field(default_factory=dict)

    def position(self, isin: str) -> PositionState:
        if isin not in self.positions:
            self.positions[isin] = PositionState()
        return self.positions[isin]

    def cached_transactions(self) -> list[Transaction]:
        return [Transaction.from_json(t) for t in self.transactions.values()]

    def merge_transactions(self, txs: list[Transaction]) -> None:
        for t in txs:
            self.transactions[t.id] = t.raw

    # ------------------------------------------------------------- notifier
    def should_notify(self, key: str, remind_after_hours: float, scope: dict[str, str] | None = None) -> bool:
        store = self.notified if scope is None else scope
        last = parse_time(store.get(key))
        if last is None:
            return True
        return utcnow() - last >= timedelta(hours=remind_after_hours)

    def mark_notified(self, key: str, scope: dict[str, str] | None = None) -> None:
        (self.notified if scope is None else scope)[key] = utcnow().isoformat()


class StateStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / "state.json"
        self.audit_path = self.dir / "audit.jsonl"
        self.lock_path = self.dir / "stopguard.lock"

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o700)

    def load(self) -> State:
        if not self.path.is_file():
            return State()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        positions = {
            isin: PositionState(**{k: v for k, v in p.items() if k in PositionState.__dataclass_fields__})
            for isin, p in raw.get("positions", {}).items()
        }
        return State(
            positions=positions,
            transactions=raw.get("transactions", {}),
            transactions_synced_at=raw.get("transactions_synced_at"),
            login_at=raw.get("login_at"),
            last_run_at=raw.get("last_run_at"),
            last_run_ok=raw.get("last_run_ok"),
            last_keepalive_at=raw.get("last_keepalive_at"),
            notified=raw.get("notified", {}),
        )

    def save(self, state: State) -> None:
        self.ensure_dir()
        payload = {"version": STATE_VERSION, **asdict(state)}
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def audit(self, event: str, **data: Any) -> None:
        self.ensure_dir()
        entry = {"at": utcnow().isoformat(), "event": event, **data}
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def read_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
        return out

    @contextlib.contextmanager
    def lock(self, wait: bool = False) -> Iterator[None]:
        """Verhindert parallele Läufe (z. B. launchd-Job + manuelles ``apply``)."""
        self.ensure_dir()
        fh = self.lock_path.open("w")
        try:
            flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fh, flags)
            except BlockingIOError as exc:
                raise LockBusy(
                    "stopguard läuft bereits (anderer Prozess hält die Sperre). Bitte kurz warten und erneut versuchen."
                ) from exc
            fh.write(str(os.getpid()))
            fh.flush()
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
