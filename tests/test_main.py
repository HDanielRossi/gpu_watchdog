"""Tests de GuardianDaemon._dispatch_action / _run_emergency_sequence.

Construir un GuardianDaemon es seguro: el constructor solo instancia
monitores/acciones (no ejecuta nvidia-smi, docker ni HTTP hasta que se
llama build_snapshot/evaluate_once, que estos tests no usan). Las acciones
reales de Docker/systemd se reemplazan por fakes que registran llamadas en
`order` para verificar la secuencia exacta, sin invocar `docker`/`shutdown`
de verdad en ningun momento.
"""
from __future__ import annotations

from datetime import datetime, timezone

from guardian.config import parse_config
from guardian.main import GuardianDaemon
from guardian.models import ActionResult, RuleEvent, Severity


class RecordingDockerActions:
    def __init__(self, order: list, raise_on=None, fail_on=None):
        self.order = order
        self.calls: list[str] = []
        self._raise_on = raise_on or set()
        self._fail_on = fail_on or set()

    def stop_container(self, name: str, reason: str) -> ActionResult:
        self.calls.append(name)
        self.order.append(f"stop_{name}")
        if name in self._raise_on:
            raise RuntimeError(f"fallo simulado deteniendo {name}")
        success = name not in self._fail_on
        return ActionResult(
            action_name="stop_container", target=name, success=success, dry_run=True,
            message="ok" if success else "fallo simulado", reason=reason,
            timestamp=datetime.now(timezone.utc), error=None if success else "fallo simulado",
        )


class RecordingSystemActions:
    def __init__(self, order: list, raise_on_shutdown: bool = False, fail: bool = False):
        self.order = order
        self.calls: list[str] = []
        self._raise_on_shutdown = raise_on_shutdown
        self._fail = fail

    def safe_shutdown(self, reason: str) -> ActionResult:
        self.calls.append("shutdown")
        self.order.append("shutdown")
        if self._raise_on_shutdown:
            raise RuntimeError("fallo simulado en shutdown")
        return ActionResult(
            action_name="safe_shutdown", target="system", success=not self._fail, dry_run=True,
            message="ok" if not self._fail else "fallo simulado", reason=reason,
            timestamp=datetime.now(timezone.utc), error=None if not self._fail else "fallo simulado",
        )


def _build_daemon(tmp_path, *, stop_comfyui=True, stop_ollama=True, shutdown=True) -> GuardianDaemon:
    raw = {
        "general": {"interval_seconds": 10, "dry_run": True, "state_file": str(tmp_path / "state.json")},
        "logging": {"file": str(tmp_path / "guardian.log")},
        "gpu": {
            "enabled": True,
            "thresholds": {"warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90},
            "actions": {
                "stop_comfyui_on_critical": stop_comfyui,
                "stop_ollama_on_emergency": stop_ollama,
                "shutdown_on_emergency": shutdown,
            },
        },
        "docker": {"enabled": True, "monitored_containers": ["comfyui", "ollama", "open-webui"]},
        "services": {"comfyui": {"container_name": "comfyui", "health_url": "http://127.0.0.1:8188/"}},
    }
    return GuardianDaemon(parse_config(raw))


def _emergency_event() -> RuleEvent:
    return RuleEvent(
        rule_name="gpu_temperature_emergency", severity=Severity.EMERGENCY, metric="gpu_temperature_c",
        value=95.0, threshold=90.0, duration_seconds=30.0, newly_triggered=True, recovered=False,
        active=True, can_act=True, timestamp=datetime.now(timezone.utc),
    )


def _critical_event() -> RuleEvent:
    return RuleEvent(
        rule_name="gpu_temperature_critical", severity=Severity.CRITICAL, metric="gpu_temperature_c",
        value=86.0, threshold=84.0, duration_seconds=60.0, newly_triggered=True, recovered=False,
        active=True, can_act=True, timestamp=datetime.now(timezone.utc),
    )


# --- secuencia completa: comfyui -> ollama -> shutdown -------------------


def test_emergency_sequence_full_order_stop_comfyui_stop_ollama_shutdown(tmp_path):
    daemon = _build_daemon(tmp_path)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    # No se disparo (ni se llamo) gpu_temperature_critical antes: EMERGENCY
    # no depende de ello.
    daemon._dispatch_action(_emergency_event())

    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]


# --- toggles individuales deshabilitados ---------------------------------


def test_emergency_sequence_shutdown_disabled_does_not_call_shutdown(tmp_path):
    daemon = _build_daemon(tmp_path, shutdown=False)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    daemon._dispatch_action(_emergency_event())

    assert order == ["stop_comfyui", "stop_ollama"]
    assert daemon._system_actions.calls == []


def test_emergency_sequence_comfyui_stop_disabled_still_stops_ollama_and_shuts_down(tmp_path):
    daemon = _build_daemon(tmp_path, stop_comfyui=False)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    daemon._dispatch_action(_emergency_event())

    assert order == ["stop_ollama", "shutdown"]
    assert "comfyui" not in daemon._docker_actions.calls


def test_emergency_sequence_ollama_stop_disabled_still_stops_comfyui_and_shuts_down(tmp_path):
    daemon = _build_daemon(tmp_path, stop_ollama=False)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    daemon._dispatch_action(_emergency_event())

    assert order == ["stop_comfyui", "shutdown"]
    assert "ollama" not in daemon._docker_actions.calls


def test_emergency_sequence_all_actions_disabled_does_nothing_and_does_not_raise(tmp_path):
    daemon = _build_daemon(tmp_path, stop_comfyui=False, stop_ollama=False, shutdown=False)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    result = daemon._dispatch_action(_emergency_event())

    assert order == []
    assert result is None


# --- fallos de un paso no deben tumbar el resto de la secuencia ---------


def test_emergency_sequence_continues_after_a_step_reports_failure(tmp_path):
    daemon = _build_daemon(tmp_path)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order, fail_on={"comfyui"})
    daemon._system_actions = RecordingSystemActions(order)

    daemon._dispatch_action(_emergency_event())

    # A pesar de que detener ComfyUI "fallo" (success=False), la secuencia
    # completa igual los siguientes pasos.
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]


def test_emergency_sequence_continues_after_unexpected_exception_in_a_step(tmp_path):
    daemon = _build_daemon(tmp_path)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order, raise_on={"comfyui"})
    daemon._system_actions = RecordingSystemActions(order)

    # No debe propagar la excepcion: la secuencia sigue y queda registrada.
    result = daemon._dispatch_action(_emergency_event())

    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]
    assert result is not None
    assert result.success is True  # el ultimo paso (shutdown) si tuvo exito


def test_emergency_sequence_records_failure_result_when_last_step_fails(tmp_path):
    daemon = _build_daemon(tmp_path)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order, raise_on_shutdown=True)

    result = daemon._dispatch_action(_emergency_event())

    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]
    assert result is not None
    assert result.success is False
    assert result.error is not None


# --- idempotencia ----------------------------------------------------


def test_emergency_sequence_is_idempotent_across_repeated_events(tmp_path):
    daemon = _build_daemon(tmp_path)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    daemon._dispatch_action(_emergency_event())
    daemon._dispatch_action(_emergency_event())

    # Recibir el mismo evento dos veces repite la secuencia de forma segura,
    # sin lanzar y sin comportamiento distinto la segunda vez.
    assert order == ["stop_comfyui", "stop_ollama", "shutdown", "stop_comfyui", "stop_ollama", "shutdown"]


# --- CRITICAL sigue funcionando de forma independiente -------------------


def test_critical_dispatch_stops_comfyui_when_enabled(tmp_path):
    daemon = _build_daemon(tmp_path)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    daemon._dispatch_action(_critical_event())

    assert order == ["stop_comfyui"]
    assert daemon._system_actions.calls == []  # CRITICAL nunca toca ollama/shutdown


def test_critical_dispatch_does_nothing_when_disabled(tmp_path):
    daemon = _build_daemon(tmp_path, stop_comfyui=False)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    result = daemon._dispatch_action(_critical_event())

    assert order == []
    assert result is None
