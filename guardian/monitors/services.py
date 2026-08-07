"""Comprobaciones HTTP de salud para ComfyUI, Ollama y Open WebUI.

Usa unicamente `urllib` de la biblioteca estandar (sin `requests`) para no
anadir una dependencia mas.
"""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from guardian.models import ServiceCheck, ServiceStatus

logger = logging.getLogger("ai_guardian.monitors.services")

_USER_AGENT = "ai-guardian-healthcheck"


class ServiceHealthChecker:
    def __init__(self, timeout: float = 3.0, slow_threshold_ms: float = 2000.0):
        self._timeout = timeout
        self._slow_threshold_ms = slow_threshold_ms

    def check(self, name: str, url: str, container_running: bool) -> ServiceCheck:
        """Comprueba la salud HTTP de un servicio.

        Si el contenedor no esta corriendo, se evita la llamada HTTP (que de
        todas formas fallaria) y se reporta directamente `stopped`.
        """
        now = datetime.now(timezone.utc)
        if not container_running:
            return ServiceCheck(name=name, status=ServiceStatus.STOPPED, timestamp=now)

        start = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                elapsed_ms = (time.monotonic() - start) * 1000
                status_code = response.status
                if status_code >= 500:
                    return ServiceCheck(
                        name=name,
                        status=ServiceStatus.CONNECTION_ERROR,
                        latency_ms=elapsed_ms,
                        http_status=status_code,
                        error=f"http {status_code}",
                        timestamp=now,
                    )
                status = ServiceStatus.SLOW if elapsed_ms > self._slow_threshold_ms else ServiceStatus.HEALTHY
                return ServiceCheck(name=name, status=status, latency_ms=elapsed_ms, http_status=status_code, timestamp=now)
        except urllib.error.HTTPError as exc:
            # Se recibio una respuesta HTTP real (aunque sea error): el servicio
            # esta arriba y contestando, solo que con un codigo de error.
            elapsed_ms = (time.monotonic() - start) * 1000
            status = ServiceStatus.CONNECTION_ERROR if exc.code >= 500 else ServiceStatus.HEALTHY
            return ServiceCheck(
                name=name,
                status=status,
                latency_ms=elapsed_ms,
                http_status=exc.code,
                error=str(exc) if status is ServiceStatus.CONNECTION_ERROR else None,
                timestamp=now,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug("service_http_check_failed name=%s url=%s error=%s", name, url, exc)
            return ServiceCheck(
                name=name,
                status=ServiceStatus.CONTAINER_UP_NO_HTTP,
                latency_ms=elapsed_ms,
                http_status=None,
                error=str(exc),
                timestamp=now,
            )
