"""Alert delivery.

Every backend takes the same Alert and fails independently, so one broken
channel never stops the others from firing.
"""

from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import requests

log = logging.getLogger("toppswatch.notify")


@dataclass
class Alert:
    title: str
    message: str
    url: str | None = None
    priority: str = "high"     # high | default | low
    tags: list[str] | None = None


class Notifier:
    """Fan-out to every configured channel."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.channels = []
        for name, builder in (
            ("ntfy", self._build_ntfy),
            ("telegram", self._build_telegram),
            ("home_assistant", self._build_home_assistant),
            ("email", self._build_email),
            ("webhook", self._build_webhook),
        ):
            section = self.config.get(name)
            if section and section.get("enabled", True):
                self.channels.append((name, builder(section)))

    # -- channel builders -------------------------------------------------

    def _build_ntfy(self, cfg: dict):
        server = cfg.get("server", "https://ntfy.sh").rstrip("/")
        topic = cfg["topic"]
        token = cfg.get("token")

        def send(alert: Alert) -> None:
            headers = {
                "Title": alert.title.encode("utf-8"),
                "Priority": {"high": "urgent", "default": "default", "low": "low"}[alert.priority],
                "Tags": ",".join(alert.tags or ["shopping_cart"]),
            }
            if alert.url:
                headers["Click"] = alert.url
                headers["Actions"] = f"view, Abrir tienda, {alert.url}"
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = requests.post(
                f"{server}/{topic}",
                data=alert.message.encode("utf-8"),
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()

        return send

    def _build_telegram(self, cfg: dict):
        token = cfg["bot_token"]
        chat_id = cfg["chat_id"]

        def send(alert: Alert) -> None:
            text = f"*{alert.title}*\n{alert.message}"
            if alert.url:
                text += f"\n\n{alert.url}"
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            response.raise_for_status()

        return send

    def _build_home_assistant(self, cfg: dict):
        """Fires a Home Assistant webhook -> automation -> mobile push."""
        webhook_url = cfg["webhook_url"]

        def send(alert: Alert) -> None:
            response = requests.post(
                webhook_url,
                json={
                    "title": alert.title,
                    "message": alert.message,
                    "url": alert.url,
                    "priority": alert.priority,
                },
                timeout=15,
            )
            response.raise_for_status()

        return send

    def _build_email(self, cfg: dict):
        def send(alert: Alert) -> None:
            msg = EmailMessage()
            msg["Subject"] = alert.title
            msg["From"] = cfg["from_addr"]
            msg["To"] = ", ".join(cfg["to_addrs"])
            body = alert.message + (f"\n\n{alert.url}" if alert.url else "")
            msg.set_content(body)

            host, port = cfg["host"], int(cfg.get("port", 587))
            if cfg.get("use_ssl"):
                server = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                server = smtplib.SMTP(host, port, timeout=20)
                if cfg.get("starttls", True):
                    server.starttls()
            with server:
                if cfg.get("username"):
                    server.login(cfg["username"], cfg["password"])
                server.send_message(msg)

        return send

    def _build_webhook(self, cfg: dict):
        url = cfg["url"]
        headers = cfg.get("headers", {})

        def send(alert: Alert) -> None:
            response = requests.post(
                url,
                data=json.dumps(
                    {
                        "title": alert.title,
                        "message": alert.message,
                        "url": alert.url,
                        "priority": alert.priority,
                    }
                ),
                headers={"Content-Type": "application/json", **headers},
                timeout=15,
            )
            response.raise_for_status()

        return send

    # -- dispatch ---------------------------------------------------------

    def send(self, alert: Alert) -> int:
        if not self.channels:
            log.warning("no notification channels configured — alert dropped: %s", alert.title)
            return 0
        delivered = 0
        for name, send in self.channels:
            try:
                send(alert)
                delivered += 1
                log.info("alert delivered via %s", name)
            except Exception as exc:  # noqa: BLE001
                log.error("alert failed via %s: %s", name, exc)
        return delivered

    @property
    def channel_names(self) -> list[str]:
        return [name for name, _ in self.channels]
