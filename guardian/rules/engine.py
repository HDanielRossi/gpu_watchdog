"""Motor de reglas con estado persistente en memoria.

Cada metrica vigilada (temperatura de GPU en sus 3 niveles, VRAM, potencia,
RAM, swap, disco, salud de contenedores) se modela como un `ThresholdWatcher`
independiente que exige que el umbral se mantenga superado durante una
duracion configurable antes de considerar la condicion "activa", aplica
histeresis para decidir cuando se considera recuperada, y aplica un cooldown
para no repetir la misma accion en cada ciclo mientras la condicion persiste.

Este modulo es puro: evalua un Snapshot y devuelve una lista de RuleEvent.
No ejecuta acciones ni notificaciones (eso vive en `guardian.main` /
`guardian.actions`), lo que lo hace trivial de probar sin mocks de Docker o
subprocess.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from guardian.config import (
    DEFAULT_DOCKER_UNHEALTHY_DURATION_SECONDS,
    DEFAULT_POWER_HYSTERESIS_MARGIN_W,
    DEFAULT_SYSTEM_COOLDOWN_SECONDS,
    DEFAULT_SYSTEM_DURATION_SECONDS,
    DEFAULT_SYSTEM_HYSTERESIS_MARGIN_GB,
    DEFAULT_SYSTEM_HYSTERESIS_MARGIN_PERCENT,
    DEFAULT_TELEMETRY_GAP_TOLERANCE_SECONDS,
    DEFAULT_VRAM_HYSTERESIS_MARGIN_PERCENT,
    AppConfig,
)
from guardian.models import RuleEvent, Severity, Snapshot

logger = logging.getLogger("ai_guardian.rules")

Comparison = Literal["max", "min"]  # "max": alerta si value >= umbral; "min": alerta si value <= umbral


@dataclass
class WatcherConfig:
    name: str
    metric: str
    severity: Severity
    threshold: float
    duration_seconds: float
    hysteresis_margin: float
    cooldown_seconds: float
    comparison: Comparison = "max"
    # Cuanto tiempo puede faltar telemetria (value=None) antes de que se
    # reinicie el progreso hacia una condicion sostenida. Un hueco corto
    # (p.ej. una lectura fallida aislada) no debe invalidar el progreso; un
    # hueco largo si, porque no hay evidencia de que la condicion se haya
    # mantenido durante ese tiempo.
    telemetry_gap_tolerance_seconds: float = DEFAULT_TELEMETRY_GAP_TOLERANCE_SECONDS


class ThresholdWatcher:
    """Vigila una unica metrica numerica con duracion, histeresis y cooldown."""

    def __init__(self, config: WatcherConfig):
        self.config = config
        self._exceeded_since: Optional[datetime] = None
        self._active = False
        self._last_action_at: Optional[datetime] = None
        # Momento de la primera lectura None consecutiva desde la ultima
        # lectura real; se usa para medir el hueco acumulado de telemetria.
        self._first_missing_at: Optional[datetime] = None

    def evaluate(self, value: Optional[float], now: datetime) -> RuleEvent:
        cfg = self.config

        # Si veniamos acumulando progreso hacia una condicion sostenida y hay
        # (o hubo) un hueco de telemetria en curso, medimos su duracion total
        # -- desde la primera lectura faltante hasta este mismo instante,
        # sea esta lectura otra faltante o la primera real tras el hueco --
        # y reiniciamos el progreso si supera la tolerancia configurada. Esto
        # cubre tanto huecos que siguen creciendo (varias lecturas None
        # seguidas) como el caso de un reinicio del daemon que carga un
        # estado persistido con un hueco ya vencido y recibe una lectura
        # real de inmediato: en ambos casos no podemos afirmar que la
        # condicion se mantuvo sostenida durante un intervalo sin mediciones.
        if self._exceeded_since is not None and self._first_missing_at is not None:
            gap = (now - self._first_missing_at).total_seconds()
            if gap > cfg.telemetry_gap_tolerance_seconds:
                logger.info(
                    "telemetry_gap_exceeded_progress_reset rule=%s metric=%s gap_seconds=%s tolerance_seconds=%s",
                    cfg.name, cfg.metric, gap, cfg.telemetry_gap_tolerance_seconds,
                )
                self._exceeded_since = None
                self._first_missing_at = None

        if value is None:
            # Sin lectura este ciclo: si ya habia progreso en curso, marca
            # (si aun no estaba marcado) el inicio del hueco de telemetria.
            # Una lectura fallida aislada nunca dispara ni resetea nada por
            # si sola (el chequeo de arriba es el que decide, con el reloj).
            if self._exceeded_since is not None and self._first_missing_at is None:
                self._first_missing_at = now
            return RuleEvent(
                rule_name=cfg.name,
                severity=cfg.severity,
                metric=cfg.metric,
                value=None,
                threshold=cfg.threshold,
                duration_seconds=cfg.duration_seconds,
                newly_triggered=False,
                recovered=False,
                active=self._active,
                can_act=False,
                timestamp=now,
            )

        # Lectura real: cierra cualquier hueco de telemetria en curso.
        self._first_missing_at = None

        exceeds = value >= cfg.threshold if cfg.comparison == "max" else value <= cfg.threshold
        recovery_point = cfg.threshold - cfg.hysteresis_margin if cfg.comparison == "max" else cfg.threshold + cfg.hysteresis_margin
        recovered_now = value < recovery_point if cfg.comparison == "max" else value > recovery_point

        newly_triggered = False
        recovered = False

        if self._active:
            if recovered_now:
                self._active = False
                self._exceeded_since = None
                recovered = True
        else:
            if exceeds:
                if self._exceeded_since is None:
                    self._exceeded_since = now
                elapsed = (now - self._exceeded_since).total_seconds()
                if elapsed >= cfg.duration_seconds:
                    self._active = True
                    newly_triggered = True
            else:
                self._exceeded_since = None

        can_act = False
        if newly_triggered:
            in_cooldown = self._last_action_at is not None and (now - self._last_action_at).total_seconds() < cfg.cooldown_seconds
            can_act = not in_cooldown
            if can_act:
                self._last_action_at = now
            else:
                logger.info(
                    "action_suppressed_cooldown rule=%s metric=%s value=%s cooldown_seconds=%s",
                    cfg.name, cfg.metric, value, cfg.cooldown_seconds,
                )

        return RuleEvent(
            rule_name=cfg.name,
            severity=cfg.severity,
            metric=cfg.metric,
            value=value,
            threshold=cfg.threshold,
            duration_seconds=cfg.duration_seconds,
            newly_triggered=newly_triggered,
            recovered=recovered,
            active=self._active,
            can_act=can_act,
            timestamp=now,
        )

    def to_state_dict(self) -> dict:
        return {
            "exceeded_since": self._exceeded_since.isoformat() if self._exceeded_since else None,
            "active": self._active,
            "last_action_at": self._last_action_at.isoformat() if self._last_action_at else None,
            "first_missing_at": self._first_missing_at.isoformat() if self._first_missing_at else None,
        }

    def load_state_dict(self, data: dict) -> None:
        exceeded_since = data.get("exceeded_since")
        last_action_at = data.get("last_action_at")
        first_missing_at = data.get("first_missing_at")
        self._exceeded_since = datetime.fromisoformat(exceeded_since) if exceeded_since else None
        self._active = bool(data.get("active", False))
        self._last_action_at = datetime.fromisoformat(last_action_at) if last_action_at else None
        self._first_missing_at = datetime.fromisoformat(first_missing_at) if first_missing_at else None


class RuleEngine:
    """Compone los ThresholdWatcher relevantes segun la configuracion cargada."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._watchers: dict[str, ThresholdWatcher] = {}
        self._build_watchers()

    def _build_watchers(self) -> None:
        gap_tolerance = self._config.general.telemetry_gap_tolerance_seconds

        gpu = self._config.gpu
        if gpu.enabled:
            t = gpu.thresholds
            cooldown = gpu.actions.action_cooldown_seconds
            self._add(WatcherConfig(
                name="gpu_temperature_warning", metric="gpu_temperature_c", severity=Severity.WARNING,
                threshold=t.warning_temperature_c, duration_seconds=t.warning_duration_seconds,
                hysteresis_margin=t.hysteresis_margin_c, cooldown_seconds=cooldown,
                telemetry_gap_tolerance_seconds=gap_tolerance,
            ))
            self._add(WatcherConfig(
                name="gpu_temperature_critical", metric="gpu_temperature_c", severity=Severity.CRITICAL,
                threshold=t.critical_temperature_c, duration_seconds=t.critical_duration_seconds,
                hysteresis_margin=t.hysteresis_margin_c, cooldown_seconds=cooldown,
                telemetry_gap_tolerance_seconds=gap_tolerance,
            ))
            self._add(WatcherConfig(
                name="gpu_temperature_emergency", metric="gpu_temperature_c", severity=Severity.EMERGENCY,
                threshold=t.emergency_temperature_c, duration_seconds=t.emergency_duration_seconds,
                hysteresis_margin=t.hysteresis_margin_c, cooldown_seconds=cooldown,
                telemetry_gap_tolerance_seconds=gap_tolerance,
            ))
            self._add(WatcherConfig(
                name="gpu_vram_high", metric="gpu_memory_percent", severity=Severity.WARNING,
                threshold=t.maximum_vram_percent, duration_seconds=DEFAULT_SYSTEM_DURATION_SECONDS,
                hysteresis_margin=DEFAULT_VRAM_HYSTERESIS_MARGIN_PERCENT, cooldown_seconds=cooldown,
                telemetry_gap_tolerance_seconds=gap_tolerance,
            ))
            self._add(WatcherConfig(
                name="gpu_power_high", metric="gpu_power_draw_w", severity=Severity.WARNING,
                threshold=t.maximum_power_watts, duration_seconds=DEFAULT_SYSTEM_DURATION_SECONDS,
                hysteresis_margin=DEFAULT_POWER_HYSTERESIS_MARGIN_W, cooldown_seconds=cooldown,
                telemetry_gap_tolerance_seconds=gap_tolerance,
            ))

        sysc = self._config.system
        self._add(WatcherConfig(
            name="system_ram_high", metric="ram_percent", severity=Severity.WARNING,
            threshold=sysc.maximum_ram_percent, duration_seconds=DEFAULT_SYSTEM_DURATION_SECONDS,
            hysteresis_margin=DEFAULT_SYSTEM_HYSTERESIS_MARGIN_PERCENT, cooldown_seconds=DEFAULT_SYSTEM_COOLDOWN_SECONDS,
            telemetry_gap_tolerance_seconds=gap_tolerance,
        ))
        self._add(WatcherConfig(
            name="system_swap_high", metric="swap_percent", severity=Severity.WARNING,
            threshold=sysc.maximum_swap_percent, duration_seconds=DEFAULT_SYSTEM_DURATION_SECONDS,
            hysteresis_margin=DEFAULT_SYSTEM_HYSTERESIS_MARGIN_PERCENT, cooldown_seconds=DEFAULT_SYSTEM_COOLDOWN_SECONDS,
            telemetry_gap_tolerance_seconds=gap_tolerance,
        ))
        self._add(WatcherConfig(
            name="system_disk_low", metric="disk_free_gb", severity=Severity.WARNING,
            threshold=sysc.minimum_free_disk_gb, duration_seconds=DEFAULT_SYSTEM_DURATION_SECONDS,
            hysteresis_margin=DEFAULT_SYSTEM_HYSTERESIS_MARGIN_GB, cooldown_seconds=DEFAULT_SYSTEM_COOLDOWN_SECONDS,
            comparison="min", telemetry_gap_tolerance_seconds=gap_tolerance,
        ))

        if self._config.docker.enabled and self._config.docker.restart_unhealthy_containers:
            for container_name in self._config.docker.monitored_containers:
                self._add(WatcherConfig(
                    name=f"docker_unhealthy_{container_name}", metric=f"docker_unhealthy_{container_name}",
                    severity=Severity.WARNING, threshold=0.5,
                    duration_seconds=DEFAULT_DOCKER_UNHEALTHY_DURATION_SECONDS,
                    hysteresis_margin=0.5, cooldown_seconds=DEFAULT_SYSTEM_COOLDOWN_SECONDS,
                    telemetry_gap_tolerance_seconds=gap_tolerance,
                ))

    def _add(self, config: WatcherConfig) -> None:
        self._watchers[config.name] = ThresholdWatcher(config)

    def evaluate(self, snapshot: Snapshot) -> list[RuleEvent]:
        """Evalua todos los watchers contra un snapshot y devuelve los eventos.

        No ejecuta acciones ni efectos secundarios: eso es responsabilidad de
        quien orquesta (guardian.main), que decide que hacer con cada evento
        segun la configuracion de acciones y el modo dry_run.
        """
        values = self._extract_values(snapshot)
        events = []
        for name, watcher in self._watchers.items():
            events.append(watcher.evaluate(values.get(watcher.config.metric), snapshot.timestamp))
        return events

    def _extract_values(self, snapshot: Snapshot) -> dict[str, Optional[float]]:
        values: dict[str, Optional[float]] = {}

        gpu_index = self._config.gpu.index
        gpu_sample = next((g for g in snapshot.gpus if g.index == gpu_index), None)
        if gpu_sample is not None:
            values["gpu_temperature_c"] = gpu_sample.temperature_c
            values["gpu_memory_percent"] = gpu_sample.memory_percent
            values["gpu_power_draw_w"] = gpu_sample.power_draw_w
        else:
            values["gpu_temperature_c"] = None
            values["gpu_memory_percent"] = None
            values["gpu_power_draw_w"] = None

        if snapshot.system is not None:
            values["ram_percent"] = snapshot.system.ram_percent
            values["swap_percent"] = snapshot.system.swap_percent
            free_gb_values = [d.free_gb for d in snapshot.system.disks]
            values["disk_free_gb"] = min(free_gb_values) if free_gb_values else None
        else:
            values["ram_percent"] = None
            values["swap_percent"] = None
            values["disk_free_gb"] = None

        for name, status in snapshot.containers.items():
            values[f"docker_unhealthy_{name}"] = 1.0 if status.health == "unhealthy" else 0.0

        return values

    def to_state_dict(self) -> dict:
        return {name: watcher.to_state_dict() for name, watcher in self._watchers.items()}

    def load_state_dict(self, data: dict) -> None:
        for name, watcher_state in (data or {}).items():
            watcher = self._watchers.get(name)
            if watcher is not None:
                try:
                    watcher.load_state_dict(watcher_state)
                except (ValueError, TypeError) as exc:
                    logger.warning("rule_state_restore_failed rule=%s error=%s", name, exc)
