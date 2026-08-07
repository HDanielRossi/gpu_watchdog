"""Monitor de GPU basado en `nvidia-smi`.

No usa NVML/pynvml para evitar una dependencia binaria extra: `nvidia-smi`
ya esta garantizado por el driver y su salida CSV es estable y facil de
parsear de forma defensiva.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from guardian.models import GpuSample
from guardian.utils.commands import CommandResult, run_command

logger = logging.getLogger("ai_guardian.monitors.gpu")

_QUERY_FIELDS = [
    "index",
    "name",
    "temperature.gpu",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "fan.speed",
    "pstate",
]

_THROTTLE_FIELDS = [
    "clocks_throttle_reasons.hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown",
    "clocks_throttle_reasons.sw_thermal_slowdown",
    "clocks_throttle_reasons.hw_power_brake_slowdown",
    "clocks_throttle_reasons.sw_power_cap",
]

_NA_VALUES = {"", "N/A", "[N/A]", "[Not Supported]"}

CommandRunner = Callable[..., CommandResult]


class GpuMonitor:
    def __init__(self, command_runner: CommandRunner = run_command, timeout: float = 5.0):
        self._run = command_runner
        self._timeout = timeout

    def read_all(self) -> list[GpuSample]:
        """Devuelve una muestra por cada GPU visible para nvidia-smi.

        Nunca lanza: si nvidia-smi no existe, hace timeout o falla, registra
        el error y devuelve una lista vacia para que el resto del ciclo
        continue sin datos de GPU.
        """
        result = self._run(
            ["nvidia-smi", f"--query-gpu={','.join(_QUERY_FIELDS)}", "--format=csv,noheader,nounits"],
            self._timeout,
        )
        if not result.ok:
            reason = result.error or result.stderr.strip() or f"codigo de salida {result.returncode}"
            logger.error("gpu_query_failed reason=%s", reason)
            return []

        samples: list[GpuSample] = []
        for line in result.stdout.strip().splitlines():
            sample = self._parse_line(line)
            if sample is None:
                continue
            reasons = self._read_throttle_reasons(sample.index)
            sample.throttle_reasons = reasons
            sample.throttle_active = bool(reasons)
            samples.append(sample)
        return samples

    def read(self, index: int) -> Optional[GpuSample]:
        for sample in self.read_all():
            if sample.index == index:
                return sample
        return None

    def _parse_line(self, line: str) -> Optional[GpuSample]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_QUERY_FIELDS):
            logger.warning("gpu_parse_unexpected_field_count line=%r expected=%d got=%d", line, len(_QUERY_FIELDS), len(parts))
            return None

        try:
            index = int(parts[0])
        except ValueError:
            logger.warning("gpu_parse_invalid_index line=%r", line)
            return None

        def num(value: str, cast: type) -> Optional[float]:
            if value in _NA_VALUES:
                return None
            try:
                return cast(value)
            except ValueError:
                return None

        return GpuSample(
            index=index,
            name=parts[1] if parts[1] not in _NA_VALUES else None,
            temperature_c=num(parts[2], float),
            utilization_percent=num(parts[3], float),
            memory_used_mb=num(parts[4], float),
            memory_total_mb=num(parts[5], float),
            power_draw_w=num(parts[6], float),
            power_limit_w=num(parts[7], float),
            fan_speed_percent=num(parts[8], float),
            pstate=parts[9] if parts[9] not in _NA_VALUES else None,
            available=True,
            error=None,
            timestamp=datetime.now(timezone.utc),
        )

    def _read_throttle_reasons(self, index: int) -> list[str]:
        result = self._run(
            ["nvidia-smi", "-i", str(index), f"--query-gpu={','.join(_THROTTLE_FIELDS)}", "--format=csv,noheader"],
            self._timeout,
        )
        if not result.ok or not result.stdout.strip():
            return []
        first_line = result.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in first_line.split(",")]
        active = []
        for field_name, value in zip(_THROTTLE_FIELDS, parts):
            if value.lower() == "active":
                active.append(field_name.replace("clocks_throttle_reasons.", ""))
        return active
