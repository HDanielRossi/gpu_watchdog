"""Carga y validacion de la configuracion YAML de AI Guardian.

Ningun secreto (tokens, chat ids) vive en el YAML: se leen desde variables
de entorno cuyo *nombre* esta declarado en la configuracion.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_CONFIG_PATH = "/etc/ai-guardian/config.yaml"

# Defaults del motor de reglas para metricas que la configuracion de ejemplo
# no gradua explicitamente (RAM, swap, disco, VRAM, potencia). Estos valores
# solo se usan cuando la clave opcional correspondiente no esta presente en
# el YAML del usuario; ver README para las claves que permiten sobreescribirlos.
DEFAULT_HYSTERESIS_MARGIN_C = 5.0
DEFAULT_GPU_ACTION_COOLDOWN_SECONDS = 300.0
DEFAULT_SYSTEM_DURATION_SECONDS = 30.0
DEFAULT_SYSTEM_HYSTERESIS_MARGIN_PERCENT = 5.0
DEFAULT_SYSTEM_HYSTERESIS_MARGIN_GB = 5.0
DEFAULT_SYSTEM_COOLDOWN_SECONDS = 300.0
DEFAULT_VRAM_HYSTERESIS_MARGIN_PERCENT = 5.0
DEFAULT_POWER_HYSTERESIS_MARGIN_W = 20.0
DEFAULT_DOCKER_UNHEALTHY_DURATION_SECONDS = 60.0


class ConfigError(Exception):
    """Error de configuracion con uno o mas mensajes claros para el operador."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class GeneralConfig:
    interval_seconds: float = 10.0
    dry_run: bool = True
    timezone: str = "UTC"
    state_file: Optional[str] = None


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "/var/log/ai-guardian/guardian.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass
class GpuThresholds:
    warning_temperature_c: float = 78.0
    critical_temperature_c: float = 84.0
    emergency_temperature_c: float = 90.0
    warning_duration_seconds: float = 30.0
    critical_duration_seconds: float = 60.0
    emergency_duration_seconds: float = 30.0
    maximum_vram_percent: float = 98.0
    maximum_power_watts: float = 350.0
    hysteresis_margin_c: float = DEFAULT_HYSTERESIS_MARGIN_C


@dataclass
class GpuActionsConfig:
    log_warning: bool = True
    stop_comfyui_on_critical: bool = False
    stop_ollama_on_emergency: bool = False
    shutdown_on_emergency: bool = False
    action_cooldown_seconds: float = DEFAULT_GPU_ACTION_COOLDOWN_SECONDS


@dataclass
class GpuConfig:
    enabled: bool = True
    index: int = 0
    thresholds: GpuThresholds = field(default_factory=GpuThresholds)
    actions: GpuActionsConfig = field(default_factory=GpuActionsConfig)


@dataclass
class SystemConfig:
    minimum_free_disk_gb: float = 20.0
    maximum_ram_percent: float = 95.0
    maximum_swap_percent: float = 80.0
    disk_paths: list[str] = field(default_factory=lambda: ["/"])


@dataclass
class DockerConfig:
    enabled: bool = True
    restart_unhealthy_containers: bool = False
    monitored_containers: list[str] = field(default_factory=list)


@dataclass
class ServiceConfig:
    enabled: bool
    container_name: str
    health_url: str


@dataclass
class ServicesConfig:
    services: dict[str, ServiceConfig] = field(default_factory=dict)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token_env: str = "AI_GUARDIAN_TELEGRAM_TOKEN"
    chat_id_env: str = "AI_GUARDIAN_TELEGRAM_CHAT_ID"

    def resolve_token(self) -> Optional[str]:
        return os.environ.get(self.bot_token_env) or None

    def resolve_chat_id(self) -> Optional[str]:
        return os.environ.get(self.chat_id_env) or None


@dataclass
class NotificationsConfig:
    enabled: bool = False
    cooldown_seconds: float = 300.0
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    gpu: GpuConfig = field(default_factory=GpuConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    @property
    def resolved_state_file(self) -> str:
        if self.general.state_file:
            return self.general.state_file
        return str(Path(self.logging.file).parent / "state.json")


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _require_dict(raw: Any, path: str, errors: list[str]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"'{path}' debe ser un mapeo (dict), se recibio: {type(raw).__name__}")
        return {}
    return raw


def _require_number(raw: Any, path: str, errors: list[str], default: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.append(f"'{path}' debe ser numerico, se recibio: {raw!r}")
        return default
    return float(raw)


def _require_int(raw: Any, path: str, errors: list[str], default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        errors.append(f"'{path}' debe ser un entero, se recibio: {raw!r}")
        return default
    return raw


def _require_bool(raw: Any, path: str, errors: list[str], default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        errors.append(f"'{path}' debe ser booleano (true/false), se recibio: {raw!r}")
        return default
    return raw


def _require_str(raw: Any, path: str, errors: list[str], default: str) -> str:
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        errors.append(f"'{path}' debe ser una cadena no vacia, se recibio: {raw!r}")
        return default
    return raw


def _parse_general(raw: Any, errors: list[str]) -> GeneralConfig:
    d = _require_dict(raw, "general", errors)
    interval = _require_number(d.get("interval_seconds"), "general.interval_seconds", errors, 10.0)
    if interval <= 0:
        errors.append("'general.interval_seconds' debe ser mayor a 0")
        interval = 10.0
    return GeneralConfig(
        interval_seconds=interval,
        dry_run=_require_bool(d.get("dry_run"), "general.dry_run", errors, True),
        timezone=_require_str(d.get("timezone"), "general.timezone", errors, "UTC"),
        state_file=d.get("state_file"),
    )


def _parse_logging(raw: Any, errors: list[str]) -> LoggingConfig:
    d = _require_dict(raw, "logging", errors)
    level = _require_str(d.get("level"), "logging.level", errors, "INFO").upper()
    if level not in _VALID_LOG_LEVELS:
        errors.append(f"'logging.level' invalido: {level!r} (validos: {sorted(_VALID_LOG_LEVELS)})")
        level = "INFO"
    max_bytes = _require_int(d.get("max_bytes"), "logging.max_bytes", errors, 10 * 1024 * 1024)
    backup_count = _require_int(d.get("backup_count"), "logging.backup_count", errors, 5)
    return LoggingConfig(
        level=level,
        file=_require_str(d.get("file"), "logging.file", errors, "/var/log/ai-guardian/guardian.log"),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def _parse_gpu(raw: Any, errors: list[str]) -> GpuConfig:
    d = _require_dict(raw, "gpu", errors)
    t = _require_dict(d.get("thresholds"), "gpu.thresholds", errors)
    a = _require_dict(d.get("actions"), "gpu.actions", errors)

    defaults = GpuThresholds()
    thresholds = GpuThresholds(
        warning_temperature_c=_require_number(t.get("warning_temperature_c"), "gpu.thresholds.warning_temperature_c", errors, defaults.warning_temperature_c),
        critical_temperature_c=_require_number(t.get("critical_temperature_c"), "gpu.thresholds.critical_temperature_c", errors, defaults.critical_temperature_c),
        emergency_temperature_c=_require_number(t.get("emergency_temperature_c"), "gpu.thresholds.emergency_temperature_c", errors, defaults.emergency_temperature_c),
        warning_duration_seconds=_require_number(t.get("warning_duration_seconds"), "gpu.thresholds.warning_duration_seconds", errors, defaults.warning_duration_seconds),
        critical_duration_seconds=_require_number(t.get("critical_duration_seconds"), "gpu.thresholds.critical_duration_seconds", errors, defaults.critical_duration_seconds),
        emergency_duration_seconds=_require_number(t.get("emergency_duration_seconds"), "gpu.thresholds.emergency_duration_seconds", errors, defaults.emergency_duration_seconds),
        maximum_vram_percent=_require_number(t.get("maximum_vram_percent"), "gpu.thresholds.maximum_vram_percent", errors, defaults.maximum_vram_percent),
        maximum_power_watts=_require_number(t.get("maximum_power_watts"), "gpu.thresholds.maximum_power_watts", errors, defaults.maximum_power_watts),
        hysteresis_margin_c=_require_number(t.get("hysteresis_margin_c"), "gpu.thresholds.hysteresis_margin_c", errors, DEFAULT_HYSTERESIS_MARGIN_C),
    )
    if not (thresholds.warning_temperature_c < thresholds.critical_temperature_c < thresholds.emergency_temperature_c):
        errors.append(
            "los umbrales de temperatura de GPU deben cumplir "
            "warning < critical < emergency "
            f"({thresholds.warning_temperature_c} < {thresholds.critical_temperature_c} < {thresholds.emergency_temperature_c})"
        )

    action_defaults = GpuActionsConfig()
    actions = GpuActionsConfig(
        log_warning=_require_bool(a.get("log_warning"), "gpu.actions.log_warning", errors, action_defaults.log_warning),
        stop_comfyui_on_critical=_require_bool(a.get("stop_comfyui_on_critical"), "gpu.actions.stop_comfyui_on_critical", errors, action_defaults.stop_comfyui_on_critical),
        stop_ollama_on_emergency=_require_bool(a.get("stop_ollama_on_emergency"), "gpu.actions.stop_ollama_on_emergency", errors, action_defaults.stop_ollama_on_emergency),
        shutdown_on_emergency=_require_bool(a.get("shutdown_on_emergency"), "gpu.actions.shutdown_on_emergency", errors, action_defaults.shutdown_on_emergency),
        action_cooldown_seconds=_require_number(a.get("action_cooldown_seconds"), "gpu.actions.action_cooldown_seconds", errors, DEFAULT_GPU_ACTION_COOLDOWN_SECONDS),
    )

    index = _require_int(d.get("index"), "gpu.index", errors, 0)
    if index < 0:
        errors.append("'gpu.index' debe ser >= 0")
        index = 0

    return GpuConfig(
        enabled=_require_bool(d.get("enabled"), "gpu.enabled", errors, True),
        index=index,
        thresholds=thresholds,
        actions=actions,
    )


def _parse_system(raw: Any, errors: list[str]) -> SystemConfig:
    d = _require_dict(raw, "system", errors)
    defaults = SystemConfig()
    disk_paths = d.get("disk_paths", defaults.disk_paths)
    if not isinstance(disk_paths, list) or not all(isinstance(p, str) for p in disk_paths):
        errors.append("'system.disk_paths' debe ser una lista de rutas (cadenas)")
        disk_paths = defaults.disk_paths
    return SystemConfig(
        minimum_free_disk_gb=_require_number(d.get("minimum_free_disk_gb"), "system.minimum_free_disk_gb", errors, defaults.minimum_free_disk_gb),
        maximum_ram_percent=_require_number(d.get("maximum_ram_percent"), "system.maximum_ram_percent", errors, defaults.maximum_ram_percent),
        maximum_swap_percent=_require_number(d.get("maximum_swap_percent"), "system.maximum_swap_percent", errors, defaults.maximum_swap_percent),
        disk_paths=disk_paths,
    )


def _parse_docker(raw: Any, errors: list[str]) -> DockerConfig:
    d = _require_dict(raw, "docker", errors)
    containers = d.get("monitored_containers", [])
    if not isinstance(containers, list) or not all(isinstance(c, str) for c in containers):
        errors.append("'docker.monitored_containers' debe ser una lista de nombres (cadenas)")
        containers = []
    return DockerConfig(
        enabled=_require_bool(d.get("enabled"), "docker.enabled", errors, True),
        restart_unhealthy_containers=_require_bool(d.get("restart_unhealthy_containers"), "docker.restart_unhealthy_containers", errors, False),
        monitored_containers=containers,
    )


def _parse_services(raw: Any, errors: list[str]) -> ServicesConfig:
    d = _require_dict(raw, "services", errors)
    services: dict[str, ServiceConfig] = {}
    for name, entry in d.items():
        entry_d = _require_dict(entry, f"services.{name}", errors)
        container_name = entry_d.get("container_name")
        health_url = entry_d.get("health_url")
        if not isinstance(container_name, str) or not container_name:
            errors.append(f"'services.{name}.container_name' es obligatorio y debe ser una cadena no vacia")
            container_name = ""
        if not isinstance(health_url, str) or not health_url:
            errors.append(f"'services.{name}.health_url' es obligatorio y debe ser una cadena no vacia")
            health_url = ""
        services[name] = ServiceConfig(
            enabled=_require_bool(entry_d.get("enabled"), f"services.{name}.enabled", errors, True),
            container_name=container_name,
            health_url=health_url,
        )
    return ServicesConfig(services=services)


def _parse_notifications(raw: Any, errors: list[str]) -> NotificationsConfig:
    d = _require_dict(raw, "notifications", errors)
    tg = _require_dict(d.get("telegram"), "notifications.telegram", errors)
    telegram_defaults = TelegramConfig()
    telegram = TelegramConfig(
        enabled=_require_bool(tg.get("enabled"), "notifications.telegram.enabled", errors, False),
        bot_token_env=_require_str(tg.get("bot_token_env"), "notifications.telegram.bot_token_env", errors, telegram_defaults.bot_token_env),
        chat_id_env=_require_str(tg.get("chat_id_env"), "notifications.telegram.chat_id_env", errors, telegram_defaults.chat_id_env),
    )
    if telegram.enabled:
        if not telegram.resolve_token():
            errors.append(
                f"'notifications.telegram.enabled' es true pero la variable de entorno "
                f"'{telegram.bot_token_env}' no esta definida"
            )
        if not telegram.resolve_chat_id():
            errors.append(
                f"'notifications.telegram.enabled' es true pero la variable de entorno "
                f"'{telegram.chat_id_env}' no esta definida"
            )
    return NotificationsConfig(
        enabled=_require_bool(d.get("enabled"), "notifications.enabled", errors, False),
        cooldown_seconds=_require_number(d.get("cooldown_seconds"), "notifications.cooldown_seconds", errors, 300.0),
        telegram=telegram,
    )


def parse_config(raw: dict) -> AppConfig:
    """Convierte un dict ya parseado de YAML en un AppConfig validado.

    Recolecta todos los errores encontrados (en lugar de fallar en el
    primero) y los lanza juntos como ConfigError para que el operador vea
    el panorama completo de una sola vez.
    """
    if not isinstance(raw, dict):
        raise ConfigError(["el archivo de configuracion debe contener un mapeo YAML (dict) en la raiz"])

    errors: list[str] = []
    config = AppConfig(
        general=_parse_general(raw.get("general"), errors),
        logging=_parse_logging(raw.get("logging"), errors),
        gpu=_parse_gpu(raw.get("gpu"), errors),
        system=_parse_system(raw.get("system"), errors),
        docker=_parse_docker(raw.get("docker"), errors),
        services=_parse_services(raw.get("services"), errors),
        notifications=_parse_notifications(raw.get("notifications"), errors),
    )

    if config.docker.enabled:
        for name in config.docker.monitored_containers:
            if not name or not name.strip():
                errors.append("'docker.monitored_containers' contiene un nombre vacio")

    if errors:
        raise ConfigError(errors)
    return config


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Lee y valida el archivo de configuracion YAML en `path`."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError([f"no se encontro el archivo de configuracion: {config_path}"])
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"no se pudo leer '{config_path}': {exc}"]) from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"YAML invalido en '{config_path}': {exc}"]) from exc
    return parse_config(raw or {})
