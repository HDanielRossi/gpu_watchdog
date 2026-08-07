"""Dataclasses y enums tipados compartidos por todo AI Guardian."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


@dataclass
class GpuSample:
    index: int
    name: Optional[str] = None
    temperature_c: Optional[float] = None
    utilization_percent: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    power_draw_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    fan_speed_percent: Optional[float] = None
    pstate: Optional[str] = None
    throttle_active: bool = False
    throttle_reasons: list[str] = field(default_factory=list)
    available: bool = True
    error: Optional[str] = None
    timestamp: Optional[datetime] = None

    @property
    def memory_percent(self) -> Optional[float]:
        if self.memory_used_mb is None or not self.memory_total_mb:
            return None
        return round((self.memory_used_mb / self.memory_total_mb) * 100, 2)


# ---------------------------------------------------------------------------
# Sistema
# ---------------------------------------------------------------------------


@dataclass
class DiskUsage:
    mountpoint: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float


@dataclass
class SystemSample:
    cpu_percent: Optional[float] = None
    cpu_temperature_c: Optional[float] = None
    load_average: Optional[tuple[float, float, float]] = None
    ram_percent: Optional[float] = None
    ram_used_gb: Optional[float] = None
    ram_total_gb: Optional[float] = None
    swap_percent: Optional[float] = None
    swap_used_gb: Optional[float] = None
    swap_total_gb: Optional[float] = None
    disks: list[DiskUsage] = field(default_factory=list)
    uptime_seconds: Optional[float] = None
    timestamp: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


@dataclass
class ContainerStatus:
    name: str
    exists: bool
    running: bool
    status: str  # running / exited / restarting / paused / not_found / unknown
    health: Optional[str] = None  # healthy / unhealthy / starting / None
    restart_count: int = 0
    started_at: Optional[datetime] = None
    cpu_percent: Optional[float] = None
    memory_mb: Optional[float] = None
    error: Optional[str] = None
    timestamp: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Servicios HTTP
# ---------------------------------------------------------------------------


class ServiceStatus(str, Enum):
    STOPPED = "stopped"
    CONTAINER_UP_NO_HTTP = "container_up_no_http"
    HEALTHY = "healthy"
    SLOW = "slow"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN = "unknown"


@dataclass
class ServiceCheck:
    name: str
    status: ServiceStatus
    latency_ms: Optional[float] = None
    http_status: Optional[int] = None
    error: Optional[str] = None
    timestamp: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Reglas / acciones
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


@dataclass
class RuleEvent:
    rule_name: str
    severity: Severity
    metric: str
    value: Optional[float]
    threshold: float
    duration_seconds: float
    newly_triggered: bool
    recovered: bool
    active: bool
    can_act: bool
    timestamp: datetime


@dataclass
class ActionResult:
    action_name: str
    target: str
    success: bool
    dry_run: bool
    message: str
    reason: str
    timestamp: datetime
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Snapshot agregado (usado por check/metrics/status y por el motor de reglas)
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    timestamp: datetime
    gpus: list[GpuSample] = field(default_factory=list)
    system: Optional[SystemSample] = None
    docker_available: bool = False
    containers: dict[str, ContainerStatus] = field(default_factory=dict)
    services: dict[str, ServiceCheck] = field(default_factory=dict)
