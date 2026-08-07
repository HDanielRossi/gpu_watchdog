"""Punto de entrada de AI Guardian: orquestador del daemon y CLI.

`GuardianDaemon` conecta monitores, motor de reglas, acciones y
notificaciones. Es deliberadamente el unico lugar donde se decide *que*
accion ejecutar para cada evento de regla, segun la configuracion de
acciones y el modo dry_run; el motor de reglas en si es puro y no conoce
Docker ni subprocess.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from guardian.actions.docker_actions import DockerActions
from guardian.actions.notifications import (
    LoggerNotifier,
    NotificationDispatcher,
    NotificationPayload,
    TelegramNotifier,
)
from guardian.actions.system_actions import SystemActions
from guardian.config import AppConfig, ConfigError, DEFAULT_CONFIG_PATH, load_config
from guardian.logger import log_event, setup_logging
from guardian.models import ActionResult, RuleEvent, Severity, Snapshot
from guardian.monitors.docker import DockerMonitor
from guardian.monitors.gpu import GpuMonitor
from guardian.monitors.services import ServiceHealthChecker
from guardian.monitors.system import SystemMonitor
from guardian.rules.engine import RuleEngine
from guardian.state import StateStore

logger = logging.getLogger("ai_guardian.main")


class GuardianDaemon:
    """Une monitores + motor de reglas + acciones + notificaciones + estado."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.server_name = socket.gethostname()

        self._gpu_monitor = GpuMonitor()
        self._system_monitor = SystemMonitor(disk_paths=config.system.disk_paths)
        self._docker_monitor = DockerMonitor()
        self._service_checker = ServiceHealthChecker()
        self._rule_engine = RuleEngine(config)

        self._docker_actions = DockerActions(dry_run=config.general.dry_run)
        self._system_actions = SystemActions(
            dry_run=config.general.dry_run,
            shutdown_enabled=config.gpu.actions.shutdown_on_emergency,
        )

        self._state_store = StateStore(config.resolved_state_file)
        self.notification_dispatcher = self._build_notification_dispatcher()

        self._restore_state()

    def _build_notification_dispatcher(self) -> Optional[NotificationDispatcher]:
        if not self.config.notifications.enabled:
            return None
        notifiers: list = [LoggerNotifier()]
        telegram = self.config.notifications.telegram
        if telegram.enabled:
            token = telegram.resolve_token()
            chat_id = telegram.resolve_chat_id()
            if token and chat_id:
                notifiers.append(TelegramNotifier(token, chat_id))
            else:
                logger.error(
                    "telegram_notifier_disabled reason=faltan_variables_de_entorno bot_token_env=%s chat_id_env=%s",
                    telegram.bot_token_env, telegram.chat_id_env,
                )
        return NotificationDispatcher(notifiers, cooldown_seconds=self.config.notifications.cooldown_seconds)

    def _restore_state(self) -> None:
        data = self._state_store.load()
        self._rule_engine.load_state_dict(data.get("rules", {}))

    # -- Recoleccion -------------------------------------------------

    def build_snapshot(self) -> Snapshot:
        now = datetime.now(timezone.utc)
        gpus = self._gpu_monitor.read_all() if self.config.gpu.enabled else []
        system = self._system_monitor.read()

        docker_available = False
        containers: dict = {}
        if self.config.docker.enabled:
            docker_available = self._docker_monitor.is_available()
            if docker_available:
                for name in self.config.docker.monitored_containers:
                    containers[name] = self._docker_monitor.get_status(name, include_stats=True)

        services: dict = {}
        for service_name, service_cfg in self.config.services.services.items():
            if not service_cfg.enabled:
                continue
            container_status = containers.get(service_cfg.container_name)
            if container_status is None and docker_available:
                container_status = self._docker_monitor.get_status(service_cfg.container_name)
            running = bool(container_status and container_status.running)
            services[service_name] = self._service_checker.check(service_name, service_cfg.health_url, running)

        return Snapshot(
            timestamp=now,
            gpus=gpus,
            system=system,
            docker_available=docker_available,
            containers=containers,
            services=services,
        )

    # -- Ciclo principal -----------------------------------------------

    def evaluate_once(self) -> tuple[Snapshot, list[RuleEvent]]:
        snapshot = self.build_snapshot()
        events = self._rule_engine.evaluate(snapshot)
        for event in events:
            self._handle_event(snapshot, event)
        self._persist_state(events)
        return snapshot, events

    def _handle_event(self, snapshot: Snapshot, event: RuleEvent) -> None:
        if event.recovered:
            log_event(logger, logging.INFO, f"{event.rule_name}_recovered", metric=event.metric, value=event.value, threshold=event.threshold)
            if self.notification_dispatcher:
                self._notify(snapshot, event, action_result=None, recovered=True)

        if not event.newly_triggered:
            return

        if event.rule_name == "gpu_temperature_warning" and not self.config.gpu.actions.log_warning:
            return

        level = {
            Severity.WARNING: logging.WARNING,
            Severity.CRITICAL: logging.ERROR,
            Severity.EMERGENCY: logging.CRITICAL,
        }[event.severity]
        log_event(
            logger, level, event.rule_name,
            metric=event.metric, value=event.value, threshold=event.threshold, duration=event.duration_seconds,
        )

        action_result = None
        if event.can_act:
            action_result = self._dispatch_action(event)

        if self.notification_dispatcher and (event.severity != Severity.WARNING or self.config.gpu.actions.log_warning):
            self._notify(snapshot, event, action_result=action_result, recovered=False)

    def _dispatch_action(self, event: RuleEvent) -> Optional[ActionResult]:
        reason = f"{event.rule_name} metric={event.metric} value={event.value} threshold={event.threshold}"

        if event.rule_name == "gpu_temperature_critical":
            if self.config.gpu.actions.stop_comfyui_on_critical:
                return self._docker_actions.stop_container("comfyui", reason=reason)
            return None

        if event.rule_name == "gpu_temperature_emergency":
            # Prioridad: detener primero las cargas de IA antes de considerar apagar.
            result = None
            if self.config.gpu.actions.stop_ollama_on_emergency:
                result = self._docker_actions.stop_container("ollama", reason=reason)
            if self.config.gpu.actions.shutdown_on_emergency:
                result = self._system_actions.safe_shutdown(reason=reason)
            return result

        if event.rule_name.startswith("docker_unhealthy_"):
            container_name = event.rule_name[len("docker_unhealthy_"):]
            if self.config.docker.enabled and self.config.docker.restart_unhealthy_containers:
                return self._docker_actions.restart_container(container_name, reason="health=unhealthy")
            return None

        return None

    def _notify(self, snapshot: Snapshot, event: RuleEvent, action_result: Optional[ActionResult], recovered: bool) -> None:
        assert self.notification_dispatcher is not None  # los llamadores ya lo verificaron
        gpu_sample = next((g for g in snapshot.gpus if g.index == self.config.gpu.index), None)
        comfyui_check = snapshot.services.get("comfyui")
        ollama_check = snapshot.services.get("ollama")
        event_label = f"{event.rule_name}_recovered" if recovered else event.rule_name
        payload = NotificationPayload(
            server_name=self.server_name,
            event=event_label,
            severity=event.severity.value,
            gpu_temperature_c=gpu_sample.temperature_c if gpu_sample else None,
            gpu_utilization_percent=gpu_sample.utilization_percent if gpu_sample else None,
            gpu_memory_percent=gpu_sample.memory_percent if gpu_sample else None,
            action=action_result.action_name if action_result else None,
            action_result=action_result.message if action_result else None,
            comfyui_status=comfyui_check.status.value if comfyui_check else None,
            ollama_status=ollama_check.status.value if ollama_check else None,
            timestamp=snapshot.timestamp,
        )
        self.notification_dispatcher.notify(key=event_label, payload=payload, now=snapshot.timestamp)

    def _persist_state(self, events: list[RuleEvent]) -> None:
        triggered = [e for e in events if e.active]
        data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": self.config.general.dry_run,
            "active_rules": [e.rule_name for e in triggered],
            "rules": self._rule_engine.to_state_dict(),
        }
        self._state_store.save(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-guardian", description="Monitoreo y proteccion para el servidor de IA")
    parser.add_argument(
        "--config",
        default=os.environ.get("AI_GUARDIAN_CONFIG", DEFAULT_CONFIG_PATH),
        help=f"ruta al archivo de configuracion YAML (default: {DEFAULT_CONFIG_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="ejecuta una sola revision y muestra un resumen")
    sub.add_parser("status", help="muestra el ultimo estado conocido persistido en disco")
    sub.add_parser("run", help="ejecuta el daemon en primer plano")
    sub.add_parser("validate-config", help="valida el archivo de configuracion YAML")
    sub.add_parser("test-notification", help="envia una notificacion de prueba")
    sub.add_parser("metrics", help="imprime las metricas actuales en formato JSON")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate-config":
        return _cmd_validate_config(args.config)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Error de configuracion ({args.config}):", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    setup_logging(config.logging)

    commands = {
        "check": _cmd_check,
        "status": _cmd_status,
        "run": _cmd_run,
        "test-notification": _cmd_test_notification,
        "metrics": _cmd_metrics,
    }
    return commands[args.command](config)


def _cmd_validate_config(path: str) -> int:
    try:
        load_config(path)
    except ConfigError as exc:
        print(f"Configuracion invalida ({path}):", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"Configuracion valida: {path}")
    return 0


def _cmd_check(config: AppConfig) -> int:
    daemon = GuardianDaemon(config)
    snapshot, events = daemon.evaluate_once()
    _print_summary(daemon, snapshot, events)
    return 0


def _cmd_metrics(config: AppConfig) -> int:
    daemon = GuardianDaemon(config)
    snapshot = daemon.build_snapshot()
    print(json.dumps(asdict(snapshot), indent=2, default=_json_default, ensure_ascii=False))
    return 0


def _cmd_status(config: AppConfig) -> int:
    store = StateStore(config.resolved_state_file)
    data = store.load()
    if not data:
        print("No hay estado previo guardado todavia (el daemon no ha corrido un ciclo aun).")
        return 0
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _cmd_test_notification(config: AppConfig) -> int:
    daemon = GuardianDaemon(config)
    if daemon.notification_dispatcher is None:
        print("Las notificaciones estan deshabilitadas ('notifications.enabled: false' en la configuracion).")
        return 1
    payload = NotificationPayload(
        server_name=daemon.server_name,
        event="test_notification",
        severity="info",
        action="test-notification",
        action_result="prueba manual disparada desde la CLI",
        timestamp=datetime.now(timezone.utc),
    )
    sent = daemon.notification_dispatcher.notify(key="test_notification", payload=payload)
    print("Notificacion de prueba enviada." if sent else "No se pudo enviar la notificacion (revisa los logs y la configuracion).")
    return 0 if sent else 1


def _cmd_run(config: AppConfig) -> int:
    daemon = GuardianDaemon(config)
    stop_requested = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        logger.info("signal_received signal=%s", signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "guardian_started interval_seconds=%s dry_run=%s",
        config.general.interval_seconds, config.general.dry_run,
    )

    while not stop_requested:
        cycle_start = time.monotonic()
        try:
            daemon.evaluate_once()
        except Exception:
            logger.exception("cycle_failed")

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, config.general.interval_seconds - elapsed)
        while remaining > 0 and not stop_requested:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    logger.info("guardian_stopped")
    return 0


def _print_summary(daemon: GuardianDaemon, snapshot: Snapshot, events: list[RuleEvent]) -> None:
    print(f"AI Guardian - {daemon.server_name} - {snapshot.timestamp.isoformat()}")
    print(f"  dry_run={daemon.config.general.dry_run}")

    if not snapshot.gpus:
        print("GPU: sin datos (nvidia-smi no disponible o fallo la consulta)")
    for gpu in snapshot.gpus:
        print(
            f"GPU {gpu.index} ({gpu.name}): temp={gpu.temperature_c}C util={gpu.utilization_percent}% "
            f"vram={gpu.memory_percent}% power={gpu.power_draw_w}W/{gpu.power_limit_w}W "
            f"fan={gpu.fan_speed_percent}% pstate={gpu.pstate} throttle={gpu.throttle_reasons or 'ninguno'}"
        )

    sysm = snapshot.system
    if sysm:
        disks = ", ".join(f"{d.mountpoint}={d.percent}% ({d.free_gb:.1f}GB libres)" for d in sysm.disks)
        cpu_temp = f"{sysm.cpu_temperature_c}C" if sysm.cpu_temperature_c is not None else "N/D"
        print(
            f"Sistema: cpu={sysm.cpu_percent}% cpu_temp={cpu_temp} ram={sysm.ram_percent}% "
            f"swap={sysm.swap_percent}% disco=[{disks}]"
        )

    print(f"Docker: {'disponible' if snapshot.docker_available else 'NO disponible'}")
    for name, status in snapshot.containers.items():
        print(f"  {name}: status={status.status} health={status.health} restarts={status.restart_count}")

    for name, check in snapshot.services.items():
        latency = f"{check.latency_ms:.0f}ms" if check.latency_ms is not None else "N/D"
        print(f"  servicio {name}: {check.status.value} ({latency})")

    active = [e for e in events if e.active]
    if active:
        print("Reglas activas:")
        for e in active:
            print(f"  - {e.rule_name} ({e.severity.value}) valor={e.value} umbral={e.threshold}")
    else:
        print("Reglas activas: ninguna")


if __name__ == "__main__":
    sys.exit(main())
