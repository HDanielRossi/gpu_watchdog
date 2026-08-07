"""Monitor de CPU, RAM, swap, disco y uptime basado en psutil."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import psutil

from guardian.models import DiskUsage, SystemSample

logger = logging.getLogger("ai_guardian.monitors.system")

_GB = 1024 ** 3

# Etiquetas comunes bajo las que psutil.sensors_temperatures() expone la
# temperatura del CPU segun plataforma. k10temp es la del chipset AMD Ryzen.
_CPU_TEMP_LABELS = ("k10temp", "coretemp", "cpu_thermal", "zenpower", "acpitz")


class SystemMonitor:
    def __init__(self, disk_paths: Optional[list[str]] = None):
        self._disk_paths = disk_paths or ["/"]
        try:
            psutil.cpu_percent(interval=None)  # primera llamada "calienta" el contador delta
        except Exception:  # pragma: no cover - defensivo, psutil no deberia lanzar aqui
            logger.debug("cpu_percent_warmup_failed", exc_info=True)

    def read(self) -> SystemSample:
        return SystemSample(
            cpu_percent=self._read_cpu_percent(),
            cpu_temperature_c=self._read_cpu_temperature(),
            load_average=self._read_load_average(),
            **self._read_memory(),
            disks=self._read_disks(),
            uptime_seconds=self._read_uptime(),
            timestamp=datetime.now(timezone.utc),
        )

    def _read_cpu_percent(self) -> Optional[float]:
        try:
            return psutil.cpu_percent(interval=None)
        except Exception as exc:
            logger.warning("cpu_percent_failed error=%s", exc)
            return None

    def _read_load_average(self) -> Optional[tuple[float, float, float]]:
        try:
            return os.getloadavg()
        except (OSError, AttributeError) as exc:
            logger.debug("load_average_unavailable error=%s", exc)
            return None

    def _read_memory(self) -> dict[str, Optional[float]]:
        try:
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            return {
                "ram_percent": vm.percent,
                "ram_used_gb": vm.used / _GB,
                "ram_total_gb": vm.total / _GB,
                "swap_percent": sm.percent,
                "swap_used_gb": sm.used / _GB,
                "swap_total_gb": sm.total / _GB,
            }
        except Exception as exc:
            logger.warning("memory_read_failed error=%s", exc)
            return {
                "ram_percent": None,
                "ram_used_gb": None,
                "ram_total_gb": None,
                "swap_percent": None,
                "swap_used_gb": None,
                "swap_total_gb": None,
            }

    def _read_disks(self) -> list[DiskUsage]:
        disks: list[DiskUsage] = []
        for path in self._disk_paths:
            try:
                usage = psutil.disk_usage(path)
            except OSError as exc:
                logger.warning("disk_usage_failed path=%s error=%s", path, exc)
                continue
            disks.append(
                DiskUsage(
                    mountpoint=path,
                    total_gb=usage.total / _GB,
                    used_gb=usage.used / _GB,
                    free_gb=usage.free / _GB,
                    percent=usage.percent,
                )
            )
        return disks

    def _read_uptime(self) -> Optional[float]:
        try:
            return time.time() - psutil.boot_time()
        except Exception as exc:
            logger.debug("uptime_unavailable error=%s", exc)
            return None

    def _read_cpu_temperature(self) -> Optional[float]:
        sensors_fn = getattr(psutil, "sensors_temperatures", None)
        if sensors_fn is None:
            return None
        try:
            temps = sensors_fn()
        except (OSError, AttributeError) as exc:
            logger.debug("cpu_temperature_unavailable error=%s", exc)
            return None
        if not temps:
            return None

        for label in _CPU_TEMP_LABELS:
            entries = temps.get(label)
            if entries:
                for entry in entries:
                    if entry.current is not None:
                        return float(entry.current)

        for entries in temps.values():
            for entry in entries:
                if entry.current is not None:
                    return float(entry.current)
        return None
