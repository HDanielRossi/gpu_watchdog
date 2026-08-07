"""Interfaz de notificaciones extensible: logger (siempre disponible) y
Telegram (opcional). Un NotificationDispatcher evita el envio repetitivo
mediante cooldown y deduplicacion por clave de evento.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

logger = logging.getLogger("ai_guardian.notifications")


@dataclass
class NotificationPayload:
    server_name: str
    event: str
    severity: str
    gpu_temperature_c: Optional[float] = None
    gpu_utilization_percent: Optional[float] = None
    gpu_memory_percent: Optional[float] = None
    action: Optional[str] = None
    action_result: Optional[str] = None
    comfyui_status: Optional[str] = None
    ollama_status: Optional[str] = None
    timestamp: Optional[datetime] = None

    def render_text(self) -> str:
        ts = (self.timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S %Z")
        lines = [
            f"AI Guardian [{self.severity.upper()}] - {self.server_name}",
            f"Fecha: {ts}",
            f"Evento: {self.event}",
        ]
        if self.gpu_temperature_c is not None:
            lines.append(f"Temperatura GPU: {self.gpu_temperature_c} C")
        if self.gpu_utilization_percent is not None:
            lines.append(f"Uso GPU: {self.gpu_utilization_percent}%")
        if self.gpu_memory_percent is not None:
            lines.append(f"VRAM: {self.gpu_memory_percent}%")
        if self.action:
            lines.append(f"Accion: {self.action}")
        if self.action_result:
            lines.append(f"Resultado: {self.action_result}")
        if self.comfyui_status:
            lines.append(f"ComfyUI: {self.comfyui_status}")
        if self.ollama_status:
            lines.append(f"Ollama: {self.ollama_status}")
        return "\n".join(lines)


class Notifier(Protocol):
    def send(self, payload: NotificationPayload) -> bool:
        ...


class LoggerNotifier:
    """Notificador que siempre funciona: registra el evento en el log."""

    def send(self, payload: NotificationPayload) -> bool:
        logger.info("notification event=%s severity=%s server=%s", payload.event, payload.severity, payload.server_name)
        return True


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 5.0):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, payload: NotificationPayload) -> bool:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self._chat_id, "text": payload.render_text()}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                ok = 200 <= response.status < 300
                if not ok:
                    logger.error("telegram_notification_failed http_status=%s", response.status)
                return ok
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Nunca registrar el token ni el chat_id en el mensaje de error.
            logger.error("telegram_notification_failed error=%s", exc)
            return False


class NotificationDispatcher:
    def __init__(self, notifiers: list[Notifier], cooldown_seconds: float = 300.0):
        self._notifiers = notifiers
        self._cooldown_seconds = cooldown_seconds
        self._last_sent_at: dict[str, datetime] = {}

    def notify(self, key: str, payload: NotificationPayload, now: Optional[datetime] = None) -> bool:
        """Envia la notificacion a todos los notificadores configurados,
        salvo que la misma `key` se haya notificado dentro del cooldown."""
        now = now or datetime.now(timezone.utc)
        last_sent = self._last_sent_at.get(key)
        if last_sent is not None and (now - last_sent).total_seconds() < self._cooldown_seconds:
            logger.debug("notification_suppressed_cooldown key=%s", key)
            return False

        any_ok = False
        for notifier in self._notifiers:
            try:
                if notifier.send(payload):
                    any_ok = True
            except Exception as exc:  # defensivo: un notificador roto no debe tumbar el ciclo
                logger.error("notifier_failed notifier=%s error=%s", type(notifier).__name__, exc)

        self._last_sent_at[key] = now
        return any_ok
