"""Benachrichtigungen: Konsole, macOS-Mitteilungszentrale, ntfy (Push aufs Handy), Telegram."""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass

from stopguard.config import NotifySettings

log = logging.getLogger(__name__)

PRIORITY = {"info": "default", "warning": "high", "urgent": "urgent"}
EMOJI = {"info": "ℹ️", "warning": "⚠️", "urgent": "🚨"}


@dataclass
class Notifier:
    settings: NotifySettings
    dry_run: bool = False

    def send(self, title: str, message: str, level: str = "info") -> list[str]:
        """Sendet über alle Kanäle; gibt Fehlermeldungen zurück (wirft nie)."""
        errors: list[str] = []
        for channel in self.settings.channels:
            try:
                if self.dry_run:
                    log.info("[dry-run] %s: %s – %s", channel, title, message)
                    continue
                getattr(self, f"_send_{channel}")(title, message, level)
            except Exception as exc:  # noqa: BLE001 – Benachrichtigung darf Lauf nie abbrechen
                errors.append(f"{channel}: {exc}")
                log.warning("Benachrichtigung über %s fehlgeschlagen: %s", channel, exc)
        return errors

    # ----------------------------------------------------------- channels
    def _send_console(self, title: str, message: str, level: str) -> None:
        log.log(logging.WARNING if level != "info" else logging.INFO, "%s %s – %s", EMOJI.get(level, ""), title, message)

    def _send_macos(self, title: str, message: str, level: str) -> None:
        if platform.system() != "Darwin":
            raise RuntimeError("macOS-Benachrichtigungen nur auf macOS verfügbar")
        if shutil.which("terminal-notifier"):
            subprocess.run(
                ["terminal-notifier", "-title", "stopguard", "-subtitle", title, "-message", message, "-group", "stopguard"]
                + (["-sound", "default"] if level == "urgent" else []),
                check=True,
                timeout=15,
            )
            return
        script = (
            f"display notification {json.dumps(message)} with title \"stopguard\" subtitle {json.dumps(title)}"
            + (' sound name "Glass"' if level == "urgent" else "")
        )
        subprocess.run(["osascript", "-e", script], check=True, timeout=15)

    def _send_ntfy(self, title: str, message: str, level: str) -> None:
        url = self.settings.ntfy_url
        if not url:
            raise RuntimeError("notify.ntfy_url ist nicht gesetzt")
        req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
        req.add_header("Title", _header_value(title))
        req.add_header("Priority", PRIORITY.get(level, "default"))
        req.add_header("Tags", {"info": "chart_with_upwards_trend", "warning": "warning", "urgent": "rotating_light"}[level])
        token = os.environ.get(self.settings.ntfy_token_env)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 – URL kommt aus eigener Konfiguration
            resp.read()

    def _send_telegram(self, title: str, message: str, level: str) -> None:
        token = os.environ.get(self.settings.telegram_token_env)
        chat = self.settings.telegram_chat_id
        if not token or not chat:
            raise RuntimeError(f"Telegram: Umgebungsvariable {self.settings.telegram_token_env} und notify.telegram_chat_id setzen")
        body = json.dumps({"chat_id": chat, "text": f"{EMOJI.get(level, '')} {title}\n{message}"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            resp.read()


def _header_value(text: str) -> str:
    """HTTP-Header sind latin-1 – Umlaute etc. als RFC-2047 (von ntfy unterstützt)."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="
