"""Tests de GuardianDaemon._dispatch_action / _run_emergency_sequence.

Construir un GuardianDaemon es seguro: el constructor solo instancia
monitores/acciones (no ejecuta nvidia-smi, docker ni HTTP hasta que se
llama build_snapshot/evaluate_once, que estos tests no usan). Las acciones
reales de Docker/systemd se reemplazan por fakes que registran llamadas en
`order` para verificar la secuencia exacta, sin invocar `docker`/`shutdown`
de verdad en ningun momento.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from guardian.config import parse_config
from guardian.main import GuardianDaemon
from guardian.models import ActionResult, GpuSample, RuleEvent, Severity, Snapshot


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
    def __init__(self, order: list, raise_on_shutdown: bool = False, fail: bool = False, fail_first_n: int = 0):
        self.order = order
        self.calls: list[str] = []
        self._raise_on_shutdown = raise_on_shutdown
        self._fail = fail
        self._fail_first_n = fail_first_n  # las primeras N llamadas "fallan" (success=False), el resto no

    def safe_shutdown(self, reason: str) -> ActionResult:
        self.calls.append("shutdown")
        self.order.append("shutdown")
        if self._raise_on_shutdown:
            raise RuntimeError("fallo simulado en shutdown")
        success = not self._fail and len(self.calls) > self._fail_first_n
        return ActionResult(
            action_name="safe_shutdown", target="system", success=success, dry_run=True,
            message="ok" if success else "fallo simulado", reason=reason,
            timestamp=datetime.now(timezone.utc), error=None if success else "fallo simulado",
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


# ===========================================================================
# Tests de integracion: RuleEngine -> GuardianDaemon._handle_event
#
# A diferencia de los tests de arriba (que construyen un RuleEvent a mano y
# llaman _dispatch_action directamente), estos conducen el flujo real:
# Snapshot -> RuleEngine.evaluate() (el ThresholdWatcher real, con su propio
# estado de duracion/cooldown) -> lista de RuleEvent -> GuardianDaemon.
# _handle_event() para cada uno, exactamente como lo hace evaluate_once().
# Esto es lo que permite comprobar que can_act/newly_triggered se recalculan
# ciclo a ciclo tal como los produce el motor de reglas, no como los
# fabricamos nosotros.
# ===========================================================================


def _gpu_snapshot(temperature_c: float, ts: datetime) -> Snapshot:
    return Snapshot(
        timestamp=ts,
        gpus=[GpuSample(index=0, temperature_c=temperature_c, timestamp=ts)],
        system=None, docker_available=False, containers={}, services={},
    )


def _run_cycle(daemon: GuardianDaemon, temperature_c: float, ts: datetime) -> None:
    snapshot = _gpu_snapshot(temperature_c, ts)
    for event in daemon._rule_engine.evaluate(snapshot):
        daemon._handle_event(snapshot, event)


def _build_retry_daemon(tmp_path, *, cooldown_seconds: float = 60.0, emergency_duration_seconds: float = 10.0) -> GuardianDaemon:
    # critical_duration_seconds se fija deliberadamente muy alto: con la
    # temperatura de prueba (95C) sostenida, CRITICAL (umbral 84) tambien
    # cumpliria su propia duracion sostenida tarde o temprano y dispararia
    # su propia llamada a stop_container("comfyui", ...) de forma
    # independiente de EMERGENCY (exactamente el diseno de v0.1.1: son
    # watchers separados). Estos tests miden especificamente el retry de la
    # secuencia de EMERGENCY, asi que CRITICAL se mantiene fuera de rango
    # de tiempo para no contaminar `order`; su comportamiento propio ya esta
    # cubierto en test_critical_dispatch_stops_comfyui_when_enabled.
    raw = {
        "general": {"interval_seconds": 10, "dry_run": True, "state_file": str(tmp_path / "state.json")},
        "logging": {"file": str(tmp_path / "guardian.log")},
        "gpu": {
            "enabled": True,
            "thresholds": {
                "warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90,
                "emergency_duration_seconds": emergency_duration_seconds,
                "critical_duration_seconds": 100000.0,
            },
            "actions": {
                "stop_comfyui_on_critical": True,
                "stop_ollama_on_emergency": True,
                "shutdown_on_emergency": True,
                "action_cooldown_seconds": cooldown_seconds,
            },
        },
        "docker": {"enabled": True, "monitored_containers": ["comfyui", "ollama", "open-webui"]},
        "services": {"comfyui": {"container_name": "comfyui", "health_url": "http://127.0.0.1:8188/"}},
    }
    return GuardianDaemon(parse_config(raw))


def test_integration_emergency_retries_after_cooldown_when_action_keeps_failing(tmp_path):
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    daemon = _build_retry_daemon(tmp_path, cooldown_seconds=60.0, emergency_duration_seconds=10.0)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order, raise_on_shutdown=True)  # shutdown falla siempre

    _run_cycle(daemon, 95.0, START)  # arranca el timer sostenido, aun no dispara
    assert order == []

    _run_cycle(daemon, 95.0, START + timedelta(seconds=11))  # EMERGENCY dispara: primer intento (falla)
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]

    # La condicion sigue activa (temperatura sigue en 95C) pero el cooldown
    # de 60s no expiro: NO debe reintentar, sin importar cuantos ciclos
    # pasen mientras siga dentro de la ventana.
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 15))
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 30))
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 45))
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]  # sin cambios

    # Cooldown expirado (61s desde el intento en t=11): reintenta la
    # secuencia completa, sin que la condicion se haya recuperado nunca.
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 61))
    assert order == [
        "stop_comfyui", "stop_ollama", "shutdown",
        "stop_comfyui", "stop_ollama", "shutdown",
    ]


def test_integration_emergency_fails_then_succeeds_on_retry(tmp_path):
    # Narrativa completa pedida: dispara -> falla -> sigue activa -> antes
    # del cooldown no reintenta -> despues del cooldown si reintenta (y
    # esta vez la accion tiene exito).
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    daemon = _build_retry_daemon(tmp_path, cooldown_seconds=30.0, emergency_duration_seconds=10.0)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order, fail_first_n=1)  # solo el primer intento falla

    _run_cycle(daemon, 95.0, START)
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11))  # primer intento: falla
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]
    assert daemon._system_actions.calls == ["shutdown"]

    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 10))  # dentro del cooldown: nada
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]

    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 31))  # cooldown expirado: reintenta y esta vez tiene exito
    assert order == ["stop_comfyui", "stop_ollama", "shutdown", "stop_comfyui", "stop_ollama", "shutdown"]
    assert len(daemon._system_actions.calls) == 2


def test_integration_no_retry_before_cooldown_expires_at_the_boundary(tmp_path):
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    daemon = _build_retry_daemon(tmp_path, cooldown_seconds=30.0, emergency_duration_seconds=10.0)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order, raise_on_shutdown=True)

    _run_cycle(daemon, 95.0, START)
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11))  # primer intento en t=11
    assert len(daemon._system_actions.calls) == 1

    # Un segundo antes del borde del cooldown (29s desde t=11, cooldown=30s):
    # todavia en cooldown (el chequeo es "elapsed < cooldown_seconds").
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 29))
    assert len(daemon._system_actions.calls) == 1  # sin reintento todavia

    # Justo en el borde (elapsed == cooldown_seconds): ya no es "< cooldown",
    # asi que se considera expirado y autoriza el reintento.
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + 30))
    assert len(daemon._system_actions.calls) == 2


def test_integration_no_action_dispatched_on_every_single_cycle_while_active(tmp_path):
    # "nunca se ejecute en cada ciclo": entre el disparo y el reintento hay
    # muchos ciclos intermedios (uno por cada interval_seconds simulado);
    # ninguno de ellos debe generar una llamada nueva.
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    daemon = _build_retry_daemon(tmp_path, cooldown_seconds=300.0, emergency_duration_seconds=10.0)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order, raise_on_shutdown=True)

    _run_cycle(daemon, 95.0, START)
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11))  # dispara
    assert len(order) == 3

    # 28 ciclos de 10s (el interval_seconds configurado), todos dentro del
    # cooldown de 300s.
    for i in range(1, 29):
        _run_cycle(daemon, 95.0, START + timedelta(seconds=11 + i * 10))
    assert len(order) == 3  # ni una sola llamada extra en 280s de ciclos


def test_integration_recovery_stops_action_dispatch_without_needing_cooldown(tmp_path):
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    daemon = _build_retry_daemon(tmp_path, cooldown_seconds=5.0, emergency_duration_seconds=10.0)
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    _run_cycle(daemon, 95.0, START)
    _run_cycle(daemon, 95.0, START + timedelta(seconds=11))  # dispara, tiene exito
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]

    # La temperatura baja por debajo del punto de recuperacion (umbral 90 -
    # histeresis default 5 = 85): la condicion se recupera y no debe
    # reintentar nada mas, aunque el cooldown ya haya expirado.
    _run_cycle(daemon, 70.0, START + timedelta(seconds=20))
    _run_cycle(daemon, 70.0, START + timedelta(seconds=200))
    assert order == ["stop_comfyui", "stop_ollama", "shutdown"]  # sin cambios


def test_integration_cooldown_state_survives_daemon_restart(tmp_path):
    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    raw = {
        "general": {"interval_seconds": 10, "dry_run": True, "state_file": str(tmp_path / "state.json")},
        "logging": {"file": str(tmp_path / "guardian.log")},
        "gpu": {
            "enabled": True,
            "thresholds": {
                "warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90,
                "emergency_duration_seconds": 10,
                # Ver comentario en _build_retry_daemon: evita que CRITICAL
                # dispare su propia accion independiente durante el test.
                "critical_duration_seconds": 100000.0,
            },
            "actions": {
                "stop_comfyui_on_critical": True, "stop_ollama_on_emergency": True,
                "shutdown_on_emergency": True, "action_cooldown_seconds": 60,
            },
        },
        "docker": {"enabled": True, "monitored_containers": ["comfyui", "ollama", "open-webui"]},
        "services": {"comfyui": {"container_name": "comfyui", "health_url": "http://127.0.0.1:8188/"}},
    }
    config = parse_config(raw)

    daemon1 = GuardianDaemon(config)
    order1: list = []
    daemon1._docker_actions = RecordingDockerActions(order1)
    daemon1._system_actions = RecordingSystemActions(order1, raise_on_shutdown=True)

    events = None
    for offset in (0, 11):
        snapshot = _gpu_snapshot(95.0, START + timedelta(seconds=offset))
        events = daemon1._rule_engine.evaluate(snapshot)
        for event in events:
            daemon1._handle_event(snapshot, event)
    assert order1 == ["stop_comfyui", "stop_ollama", "shutdown"]

    daemon1._persist_state(events)  # lo mismo que hace evaluate_once() al final de cada ciclo

    # "Reinicio": una instancia nueva de GuardianDaemon que carga el mismo
    # archivo de estado persistido (igual que systemd al reiniciar el
    # servicio).
    daemon2 = GuardianDaemon(config)
    order2: list = []
    daemon2._docker_actions = RecordingDockerActions(order2)
    daemon2._system_actions = RecordingSystemActions(order2, raise_on_shutdown=True)

    # 30s despues del intento original (t=11): el cooldown de 60s sigue
    # vigente incluso tras el "reinicio" -> no debe reintentar.
    _run_cycle(daemon2, 95.0, START + timedelta(seconds=11 + 30))
    assert order2 == []

    # 61s despues del intento original: el cooldown si expiro -> reintenta,
    # demostrando que el estado de cooldown sobrevivio al reinicio.
    _run_cycle(daemon2, 95.0, START + timedelta(seconds=11 + 61))
    assert order2 == ["stop_comfyui", "stop_ollama", "shutdown"]


def test_integration_warning_never_dispatches_destructive_actions_even_after_cooldown(tmp_path):
    raw = {
        "general": {"interval_seconds": 10, "dry_run": True, "state_file": str(tmp_path / "state.json")},
        "logging": {"file": str(tmp_path / "guardian.log")},
        "gpu": {
            "enabled": True,
            "thresholds": {
                # critical/emergency muy altos (pero dentro del rango valido
                # 0-150): 80C nunca los alcanza, solo warning.
                "warning_temperature_c": 78, "critical_temperature_c": 120, "emergency_temperature_c": 140,
                "warning_duration_seconds": 5,
            },
            "actions": {"action_cooldown_seconds": 20, "log_warning": True},
        },
        "docker": {"enabled": True, "monitored_containers": ["comfyui", "ollama", "open-webui"]},
        "services": {"comfyui": {"container_name": "comfyui", "health_url": "http://127.0.0.1:8188/"}},
    }
    daemon = GuardianDaemon(parse_config(raw))
    order: list = []
    daemon._docker_actions = RecordingDockerActions(order)
    daemon._system_actions = RecordingSystemActions(order)

    START = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset in (0, 6, 6 + 25, 6 + 50):  # cruza de sobra el cooldown de 20s
        _run_cycle(daemon, 80.0, START + timedelta(seconds=offset))

    assert order == []  # WARNING jamas dispara docker stop / shutdown, con o sin retry
