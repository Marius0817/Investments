"""Konfiguration (TOML) inkl. Strategien und Positionen."""

from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = (
    Path(os.environ.get("STOPGUARD_CONFIG", "")) if os.environ.get("STOPGUARD_CONFIG") else None,
    Path.cwd() / "stopguard.toml",
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "stopguard" / "config.toml",
)

ORDER_TYPES = ("auto", "stop", "stop-limit", "trailing-stop")
EXECUTIONS = ("order", "alert", "watch")
VENUES = ("gettex", "eix", "xetra")


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Strategien
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    """Aktivierungsbedingung einer Phase – alle gesetzten Felder müssen erfüllt sein."""

    irr_pct: float | None = None  # IRR p.a. (XIRR) in %
    gain_pct: float | None = None  # Kursgewinn ggü. Einstandskurs in %
    total_return_pct: float | None = None  # Gesamtrendite inkl. Ausschüttungen in %
    min_holding_days: int | None = None
    price_above: float | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in vars(self).values())

    def describe(self) -> str:
        parts = []
        if self.irr_pct is not None:
            parts.append(f"IRR ≥ {self.irr_pct:g}% p.a.")
        if self.gain_pct is not None:
            parts.append(f"Kursgewinn ≥ {self.gain_pct:g}%")
        if self.total_return_pct is not None:
            parts.append(f"Gesamtrendite ≥ {self.total_return_pct:g}%")
        if self.min_holding_days is not None:
            parts.append(f"Haltedauer ≥ {self.min_holding_days} Tage")
        if self.price_above is not None:
            parts.append(f"Kurs ≥ {self.price_above:g}")
        return " und ".join(parts) if parts else "immer"


@dataclass
class Phase:
    name: str
    activate: Condition = field(default_factory=Condition)
    sticky: bool = True
    # Stop-Komponenten (wirksam ist der HÖCHSTE Wert aller gesetzten Komponenten)
    max_loss_pct: float | None = None  # Stop = Einstand × (1 − x %)
    trailing_pct: float | None = None  # Stop = Hochpunkt seit Phasenbeginn × (1 − x %)
    irr_floor_pct: float | None = None  # Stop = Kurs, bei dem die IRR genau x % p.a. ist
    lock_gain_pct: float | None = None  # Stop = Einstand × (1 + x %), 0 = Break-even
    stop_price: float | None = None  # fester Stop-Kurs
    order_type: str = "auto"
    limit_offset_pct: float = 1.0  # nur für stop-limit: Limit = Stop × (1 − x %)

    def has_protection(self) -> bool:
        return any(
            v is not None
            for v in (self.max_loss_pct, self.trailing_pct, self.irr_floor_pct, self.lock_gain_pct, self.stop_price)
        )

    def trailing_only(self) -> bool:
        return self.trailing_pct is not None and all(
            v is None for v in (self.max_loss_pct, self.irr_floor_pct, self.lock_gain_pct, self.stop_price)
        )

    def describe(self) -> str:
        parts = []
        if self.max_loss_pct is not None:
            parts.append(f"max. {self.max_loss_pct:g}% Verlust ggü. Einstand")
        if self.trailing_pct is not None:
            parts.append(f"Trailing {self.trailing_pct:g}% unter Hoch")
        if self.irr_floor_pct is not None:
            parts.append(f"IRR-Untergrenze {self.irr_floor_pct:g}% p.a.")
        if self.lock_gain_pct is not None:
            parts.append(f"Gewinn ≥ {self.lock_gain_pct:g}% sichern")
        if self.stop_price is not None:
            parts.append(f"fester Stop {self.stop_price:g}")
        return ", ".join(parts) if parts else "kein Stop (Verluste unbegrenzt)"


@dataclass
class Strategy:
    name: str
    phases: list[Phase]
    description: str = ""


BUILTIN_STRATEGIES: dict[str, dict[str, Any]] = {
    # Problem 3: bis 20 % Verlust tolerieren, ab erstmals 5 % IRR max. 5 % unter Hoch
    "irr_gate": {
        "description": "Bis max. 20 % Verlust tolerieren; sobald erstmals 5 % IRR p.a. erreicht sind, "
        "Verlust auf max. 5 % unter dem Höchstkurs begrenzen (Trailing).",
        "phases": [
            {"name": "Aufbau", "max_loss_pct": 20},
            {
                "name": "Gewinne sichern",
                "activate": {"irr_pct": 5, "min_holding_days": 30},
                "trailing_pct": 5,
            },
        ],
    },
    # Problem 3 (Variante): Verluste unbegrenzt, bis 5 % IRR erreicht sind
    "irr_gate_open": {
        "description": "Kein Stop, bis erstmals 5 % IRR p.a. erreicht sind; danach Trailing 5 %.",
        "phases": [
            {"name": "Laufen lassen"},
            {
                "name": "Gewinne sichern",
                "activate": {"irr_pct": 5, "min_holding_days": 30},
                "trailing_pct": 5,
            },
        ],
    },
    # Problem 2: Stop wandert immer mit – max. 5 % unter dem Hoch
    "trailing_5": {
        "description": "Stop immer max. 5 % unter dem Höchstkurs (nachgezogen).",
        "phases": [{"name": "Trailing", "trailing_pct": 5}],
    },
    # Problem 1: Stop nach IRR – Mindestrendite p.a. absichern
    "irr_lock": {
        "description": "Zunächst max. 15 % Verlust; ab 8 % IRR p.a. (nach 90 Tagen) mindestens 4 % IRR p.a. "
        "absichern, zusätzlich max. 10 % unter Hoch.",
        "phases": [
            {"name": "Aufbau", "max_loss_pct": 15},
            {
                "name": "Rendite sichern",
                "activate": {"irr_pct": 8, "min_holding_days": 90},
                "irr_floor_pct": 4,
                "trailing_pct": 10,
            },
        ],
    },
    "watch_only": {
        "description": "Nur beobachten – keine Stops.",
        "phases": [{"name": "Beobachten"}],
    },
}


# ---------------------------------------------------------------------------
# Hauptkonfiguration
# ---------------------------------------------------------------------------

@dataclass
class ScalableSettings:
    sc_command: list[str] = field(default_factory=lambda: ["sc"])
    portfolio_id: str | None = None
    quote_source: str = "consolidated"
    venue: str = "gettex"
    timeout_seconds: int = 90


@dataclass
class EngineSettings:
    price_basis: str = "bid"
    min_change_pct: float = 1.0
    min_distance_pct: float = 1.0
    only_raise: bool = True
    price_decimals: int = 2
    exit_costs_eur: float = 0.0
    renew_before_days: int = 14
    unlisted_positions: str = "watch"  # ignore | watch
    adopt_existing_orders: bool = True
    on_breach: str = "propose_sell"  # notify | propose_sell
    default_strategy: str = "irr_gate"
    default_execution: str = "order"


@dataclass
class ScheduleSettings:
    interval_minutes: int = 30
    active_hours: tuple[time, time] | None = (time(7, 30), time(22, 30))
    weekdays_only: bool = True
    timezone: str = "Europe/Berlin"
    keepalive_hours: float = 6.0  # Session-Leerlauf-Timeout der Scalable CLI: 24 h


@dataclass
class NotifySettings:
    channels: list[str] = field(default_factory=lambda: ["console"])
    remind_after_hours: float = 24
    ntfy_url: str | None = None
    ntfy_token_env: str = "STOPGUARD_NTFY_TOKEN"
    telegram_token_env: str = "STOPGUARD_TELEGRAM_TOKEN"
    telegram_chat_id: str | None = None


@dataclass
class PositionConfig:
    isin: str
    strategy: str
    execution: str
    protect_pct: float = 100.0
    enabled: bool = True
    label: str | None = None
    entry_date: datetime | None = None
    entry_price: Decimal | None = None
    venue: str | None = None
    explicit: bool = True  # False = automatisch aus Default erzeugt


@dataclass
class Config:
    scalable: ScalableSettings
    engine: EngineSettings
    schedule: ScheduleSettings
    notify: NotifySettings
    strategies: dict[str, Strategy]
    positions: dict[str, PositionConfig]
    state_dir: Path
    source: Path | None = None

    def position_for(self, isin: str) -> PositionConfig | None:
        pos = self.positions.get(isin)
        if pos is not None:
            return pos if pos.enabled else None
        if self.engine.unlisted_positions == "watch":
            return PositionConfig(
                isin=isin,
                strategy=self.engine.default_strategy,
                execution="watch",
                explicit=False,
            )
        return None

    def strategy(self, name: str) -> Strategy:
        try:
            return self.strategies[name]
        except KeyError:
            raise ConfigError(f"Unbekannte Strategie '{name}'. Verfügbar: {', '.join(sorted(self.strategies))}") from None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _num(section: dict[str, Any], key: str, where: str, default: Any = None) -> float | None:
    value = section.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} muss eine Zahl sein (ist: {value!r})")
    return float(value)


def _typed(defaults: Any, raw: dict[str, Any], where: str) -> dict[str, Any]:
    """Prüft Werte gegen den Typ des Standardwerts (int/float austauschbar)."""
    merged = dict(vars(defaults))
    for key, value in raw.items():
        default = merged.get(key)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if ok and isinstance(default, int) and not isinstance(default, bool) and isinstance(value, float):
                ok = value.is_integer()
                value = int(value)
        elif isinstance(default, str):
            ok = isinstance(value, str)
        elif isinstance(default, list):
            ok = isinstance(value, list)
        else:
            ok = value is None or isinstance(value, (str, int, float))
        if not ok:
            raise ConfigError(f"[{where}] {key}: unerwarteter Typ ({value!r})")
        merged[key] = value
    return merged


def _check_keys(section: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"Unbekannte Schlüssel in [{where}]: {', '.join(sorted(unknown))}")


def parse_phase(raw: dict[str, Any], where: str, index: int) -> Phase:
    allowed = {
        "name", "activate", "sticky", "max_loss_pct", "trailing_pct", "irr_floor_pct",
        "lock_gain_pct", "stop_price", "order_type", "limit_offset_pct",
    }
    _check_keys(raw, allowed, where)
    act_raw = raw.get("activate") or {}
    if not isinstance(act_raw, dict):
        raise ConfigError(f"{where}.activate muss eine Tabelle sein, z. B. {{ irr_pct = 5 }}")
    _check_keys(act_raw, {"irr_pct", "gain_pct", "total_return_pct", "min_holding_days", "price_above"}, f"{where}.activate")
    cond = Condition(
        irr_pct=_num(act_raw, "irr_pct", where),
        gain_pct=_num(act_raw, "gain_pct", where),
        total_return_pct=_num(act_raw, "total_return_pct", where),
        min_holding_days=int(act_raw["min_holding_days"]) if "min_holding_days" in act_raw else None,
        price_above=_num(act_raw, "price_above", where),
    )
    phase = Phase(
        name=str(raw.get("name") or f"Phase {index + 1}"),
        activate=cond,
        sticky=bool(raw.get("sticky", True)),
        max_loss_pct=_num(raw, "max_loss_pct", where),
        trailing_pct=_num(raw, "trailing_pct", where),
        irr_floor_pct=_num(raw, "irr_floor_pct", where),
        lock_gain_pct=_num(raw, "lock_gain_pct", where),
        stop_price=_num(raw, "stop_price", where),
        order_type=str(raw.get("order_type", "auto")),
        limit_offset_pct=_num(raw, "limit_offset_pct", where, 1.0) or 1.0,
    )
    if phase.order_type not in ORDER_TYPES:
        raise ConfigError(f"{where}.order_type muss einer von {ORDER_TYPES} sein")
    if phase.order_type == "trailing-stop" and not phase.trailing_only():
        raise ConfigError(
            f"{where}: order_type = 'trailing-stop' (natives Trailing beim Broker) ist nur mit "
            "trailing_pct als einziger Komponente möglich"
        )
    if phase.trailing_pct is not None and not (0.5 <= phase.trailing_pct < 100):
        raise ConfigError(f"{where}.trailing_pct muss zwischen 0,5 und 100 liegen (Scalable-Minimum 0,5 %)")
    if phase.max_loss_pct is not None and not (0 < phase.max_loss_pct < 100):
        raise ConfigError(f"{where}.max_loss_pct muss zwischen 0 und 100 liegen")
    return phase


def parse_strategy(name: str, raw: dict[str, Any]) -> Strategy:
    where = f"strategies.{name}"
    _check_keys(raw, {"description", "phases"}, where)
    phases_raw = raw.get("phases")
    if not phases_raw or not isinstance(phases_raw, list):
        raise ConfigError(f"[{where}] braucht mindestens eine Phase ([[{where}.phases]])")
    phases = [parse_phase(p, f"{where}.phases[{i}]", i) for i, p in enumerate(phases_raw)]
    if not phases[0].activate.is_empty():
        raise ConfigError(f"{where}: die erste Phase gilt immer und darf keine 'activate'-Bedingung haben")
    for i, p in enumerate(phases[1:], start=1):
        if p.activate.is_empty():
            raise ConfigError(f"{where}.phases[{i}] braucht eine 'activate'-Bedingung")
    return Strategy(name=name, phases=phases, description=str(raw.get("description", "")))


def _parse_hours(value: Any) -> tuple[time, time] | None:
    if value in (None, "", "always", "immer"):
        return None
    try:
        start, end = str(value).split("-")
        return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
    except ValueError as exc:
        raise ConfigError(f"schedule.active_hours: Format 'HH:MM-HH:MM' erwartet (ist: {value!r})") from exc


def _parse_date(value: Any, where: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        from datetime import date

        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, 12, tzinfo=timezone.utc)
        d = datetime.fromisoformat(str(value))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ConfigError(f"{where}: Datum im Format JJJJ-MM-TT erwartet") from exc


def load_config(path: Path | None = None) -> Config:
    if path is None:
        for candidate in DEFAULT_CONFIG_PATHS:
            if candidate is not None and candidate.is_file():
                path = candidate
                break
    raw: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")
        with path.open("rb") as fh:
            try:
                raw = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path}: ungültiges TOML – {exc}") from exc
    return parse_config(raw, source=path)


def parse_config(raw: dict[str, Any], source: Path | None = None) -> Config:
    _check_keys(raw, {"scalable", "engine", "schedule", "notify", "paths", "strategies", "positions"}, "root")

    sc_raw = raw.get("scalable", {})
    _check_keys(sc_raw, {"sc_command", "portfolio_id", "quote_source", "venue", "timeout_seconds"}, "scalable")
    sc_cmd = sc_raw.get("sc_command", "sc")
    sc_command = shlex.split(sc_cmd) if isinstance(sc_cmd, str) else [str(x) for x in sc_cmd]
    venue = str(sc_raw.get("venue", "gettex")).lower()
    if venue not in VENUES:
        raise ConfigError(f"scalable.venue muss einer von {VENUES} sein")
    scalable = ScalableSettings(
        sc_command=sc_command,
        portfolio_id=sc_raw.get("portfolio_id") or None,
        quote_source=str(sc_raw.get("quote_source", "consolidated")),
        venue=venue,
        timeout_seconds=int(sc_raw.get("timeout_seconds", 90)),
    )

    en_raw = raw.get("engine", {})
    _check_keys(en_raw, set(EngineSettings.__dataclass_fields__), "engine")
    engine = EngineSettings(**_typed(EngineSettings(), en_raw, "engine"))
    if engine.price_basis not in ("bid", "mid"):
        raise ConfigError("engine.price_basis muss 'bid' oder 'mid' sein")
    if engine.unlisted_positions not in ("ignore", "watch"):
        raise ConfigError("engine.unlisted_positions muss 'ignore' oder 'watch' sein")
    if engine.on_breach not in ("notify", "propose_sell"):
        raise ConfigError("engine.on_breach muss 'notify' oder 'propose_sell' sein")
    if engine.default_execution not in EXECUTIONS:
        raise ConfigError(f"engine.default_execution muss einer von {EXECUTIONS} sein")

    sch_raw = raw.get("schedule", {})
    _check_keys(sch_raw, {"interval_minutes", "active_hours", "weekdays_only", "timezone", "keepalive_hours"}, "schedule")
    schedule = ScheduleSettings(
        interval_minutes=int(sch_raw.get("interval_minutes", 30)),
        active_hours=_parse_hours(sch_raw.get("active_hours", "07:30-22:30")),
        weekdays_only=bool(sch_raw.get("weekdays_only", True)),
        timezone=str(sch_raw.get("timezone", "Europe/Berlin")),
        keepalive_hours=float(sch_raw.get("keepalive_hours", 6.0)),
    )
    if not 0 < schedule.keepalive_hours < 24:
        raise ConfigError("schedule.keepalive_hours muss zwischen 0 und 24 liegen (Leerlauf-Timeout der CLI: 24 h)")

    no_raw = raw.get("notify", {})
    _check_keys(no_raw, set(NotifySettings.__dataclass_fields__), "notify")
    notify = NotifySettings(**_typed(NotifySettings(), no_raw, "notify"))
    for ch in notify.channels:
        if ch not in ("console", "macos", "ntfy", "telegram"):
            raise ConfigError(f"notify.channels: unbekannter Kanal '{ch}' (console, macos, ntfy, telegram)")

    strategies: dict[str, Strategy] = {
        name: parse_strategy(name, spec) for name, spec in BUILTIN_STRATEGIES.items()
    }
    for name, spec in (raw.get("strategies") or {}).items():
        strategies[name] = parse_strategy(name, spec)
    if engine.default_strategy not in strategies:
        raise ConfigError(f"engine.default_strategy '{engine.default_strategy}' ist nicht definiert")

    positions: dict[str, PositionConfig] = {}
    for i, p in enumerate(raw.get("positions") or []):
        where = f"positions[{i}]"
        _check_keys(
            p,
            {"isin", "strategy", "execution", "protect_pct", "enabled", "label", "entry_date", "entry_price", "venue"},
            where,
        )
        isin = str(p.get("isin", "")).strip().upper()
        if len(isin) != 12:
            raise ConfigError(f"{where}.isin fehlt oder ist keine gültige ISIN")
        strategy = str(p.get("strategy", engine.default_strategy))
        if strategy not in strategies:
            raise ConfigError(f"{where}: unbekannte Strategie '{strategy}'")
        execution = str(p.get("execution", engine.default_execution))
        if execution not in EXECUTIONS:
            raise ConfigError(f"{where}.execution muss einer von {EXECUTIONS} sein")
        protect = float(p.get("protect_pct", 100))
        if not 0 < protect <= 100:
            raise ConfigError(f"{where}.protect_pct muss zwischen 0 und 100 liegen")
        venue_p = p.get("venue")
        if venue_p is not None and str(venue_p).lower() not in VENUES:
            raise ConfigError(f"{where}.venue muss einer von {VENUES} sein")
        positions[isin] = PositionConfig(
            isin=isin,
            strategy=strategy,
            execution=execution,
            protect_pct=protect,
            enabled=bool(p.get("enabled", True)),
            label=p.get("label"),
            entry_date=_parse_date(p.get("entry_date"), where + ".entry_date"),
            entry_price=Decimal(str(p["entry_price"])) if p.get("entry_price") is not None else None,
            venue=str(venue_p).lower() if venue_p else None,
        )

    paths_raw = raw.get("paths", {})
    _check_keys(paths_raw, {"state_dir"}, "paths")
    default_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "stopguard"
    state_dir = Path(os.path.expanduser(str(paths_raw.get("state_dir", default_state))))
    if os.environ.get("STOPGUARD_STATE_DIR"):
        state_dir = Path(os.environ["STOPGUARD_STATE_DIR"])

    return Config(
        scalable=scalable,
        engine=engine,
        schedule=schedule,
        notify=notify,
        strategies=strategies,
        positions=positions,
        state_dir=state_dir,
        source=source,
    )
