"""Monitor de Docker basado en comandos CLI bien encapsulados.

Se opto por envolver `docker inspect` / `docker stats` en vez de anadir la
dependencia del SDK docker-py: el usuario del servicio ya necesita acceso al
socket de Docker para el CLI, y evita otra libreria que mantener al dia con
la API del daemon.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from guardian.models import ContainerStatus
from guardian.utils.commands import CommandResult, run_command

logger = logging.getLogger("ai_guardian.monitors.docker")

CommandRunner = Callable[..., CommandResult]

_NOT_FOUND_MARKERS = ("no such object", "no such container", "no such image")


class DockerMonitor:
    def __init__(self, command_runner: CommandRunner = run_command, timeout: float = 5.0, stats_timeout: float = 8.0):
        self._run = command_runner
        self._timeout = timeout
        self._stats_timeout = stats_timeout

    def is_available(self) -> bool:
        result = self._run(["docker", "info", "--format", "{{.ServerVersion}}"], self._timeout)
        if not result.ok:
            logger.error("docker_unavailable reason=%s", result.error or result.stderr.strip())
            return False
        return True

    def get_status(self, name: str, include_stats: bool = False) -> ContainerStatus:
        """Consulta el estado de un contenedor. Nunca lanza: cualquier fallo
        se refleja como ContainerStatus(exists=False, error=...)."""
        result = self._run(["docker", "inspect", name], self._timeout)
        now = datetime.now(timezone.utc)

        if not result.ok:
            stderr_lower = (result.stderr or "").lower()
            if any(marker in stderr_lower for marker in _NOT_FOUND_MARKERS):
                # Docker respondio y confirmo que el contenedor no existe:
                # esto SI es una confirmacion de "no esta corriendo".
                return ContainerStatus(name=name, exists=False, running=False, status="not_found", timestamp=now)
            error = result.error or result.stderr.strip() or "fallo desconocido al inspeccionar el contenedor"
            logger.error("docker_inspect_failed container=%s error=%s", name, error)
            # No se pudo consultar Docker (timeout, socket inaccesible, etc.):
            # estado desconocido, NUNCA asumir que esta detenido.
            return ContainerStatus(name=name, exists=False, running=None, status="unknown", error=error, timestamp=now)

        try:
            data = json.loads(result.stdout)[0]
        except (json.JSONDecodeError, IndexError, KeyError) as exc:
            logger.error("docker_inspect_parse_failed container=%s error=%s", name, exc)
            return ContainerStatus(name=name, exists=False, running=None, status="unknown", error=str(exc), timestamp=now)

        state = data.get("State", {}) or {}
        status = state.get("Status", "unknown")
        health_block = state.get("Health")
        health = health_block.get("Status") if isinstance(health_block, dict) else None
        restart_count = data.get("RestartCount", 0)
        started_at = _parse_docker_time(state.get("StartedAt"))

        cpu_percent: Optional[float] = None
        memory_mb: Optional[float] = None
        if include_stats and status == "running":
            cpu_percent, memory_mb = self._read_stats(name)

        return ContainerStatus(
            name=name,
            exists=True,
            running=status == "running",
            status=status,
            health=health,
            restart_count=restart_count if isinstance(restart_count, int) else 0,
            started_at=started_at,
            cpu_percent=cpu_percent,
            memory_mb=memory_mb,
            error=None,
            timestamp=now,
        )

    def _read_stats(self, name: str) -> tuple[Optional[float], Optional[float]]:
        result = self._run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", name],
            self._stats_timeout,
        )
        if not result.ok or not result.stdout.strip():
            if not result.ok:
                reason = result.error or result.stderr.strip() or f"codigo de salida {result.returncode}"
                logger.warning("docker_stats_failed container=%s error=%s", name, reason)
            return None, None
        try:
            data = json.loads(result.stdout.strip().splitlines()[0])
            cpu_percent = _parse_percent(data.get("CPUPerc"))
            mem_field = data.get("MemUsage", "")
            memory_mb = _parse_mem_to_mb(mem_field.split("/")[0].strip()) if mem_field else None
            return cpu_percent, memory_mb
        except (json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
            logger.warning("docker_stats_parse_failed container=%s error=%s", name, exc)
            return None, None


_DOCKER_TIME_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?$"
)


def _parse_docker_time(raw: Optional[str]) -> Optional[datetime]:
    """Convierte una marca de tiempo RFC3339 de Docker (con nanosegundos) a datetime."""
    if not raw or raw.startswith("0001-01-01"):
        return None
    match = _DOCKER_TIME_RE.match(raw)
    if not match:
        return None
    base = match.group("base")
    frac = (match.group("frac") or "").ljust(6, "0")[:6]
    tz = match.group("tz") or "Z"
    tz = "+00:00" if tz == "Z" else tz
    try:
        return datetime.fromisoformat(f"{base}.{frac}{tz}")
    except ValueError:
        return None


def _parse_percent(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(raw.rstrip("%"))
    except ValueError:
        return None


_MEM_UNITS = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3}


def _parse_mem_to_mb(raw: str) -> Optional[float]:
    raw = raw.strip()
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):
        if raw.lower().endswith(suffix):
            number_part = raw[: -len(suffix)].strip()
            try:
                value = float(number_part)
            except ValueError:
                return None
            return value * _MEM_UNITS[suffix] / (1024 ** 2)
    return None
