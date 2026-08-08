from guardian.actions.docker_actions import DockerActions
from guardian.actions.system_actions import SystemActions
from guardian.monitors.docker import DockerMonitor
from guardian.utils.commands import CommandResult


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="", timed_out=False, error=None)


def _fail(stderr: str = "", error=None, returncode=1) -> CommandResult:
    return CommandResult(args=(), returncode=returncode, stdout="", stderr=stderr, timed_out=False, error=error)


class RecordingRunner:
    def __init__(self, result: CommandResult):
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=5.0, input_text=None):
        self.calls.append(list(args))
        return self.result


# --- dry_run ----------------------------------------------------------


def test_dry_run_does_not_invoke_docker_and_reports_success():
    runner = RecordingRunner(_ok())
    actions = DockerActions(dry_run=True, command_runner=runner)

    result = actions.stop_container("comfyui", reason="test")

    assert result.dry_run is True
    assert result.success is True
    assert "se habria ejecutado" in result.message
    assert runner.calls == []  # nunca se llamo a docker de verdad


def test_dry_run_applies_to_restart_and_stop_containers_too():
    runner = RecordingRunner(_ok())
    actions = DockerActions(dry_run=True, command_runner=runner)

    restart_result = actions.restart_container("ollama", reason="test")
    stop_results = actions.stop_containers(["comfyui", "ollama"], reason="test")

    assert restart_result.dry_run is True
    assert all(r.dry_run is True for r in stop_results)
    assert runner.calls == []


def test_real_run_invokes_docker_stop_when_dry_run_disabled():
    runner = RecordingRunner(_ok())
    actions = DockerActions(dry_run=False, command_runner=runner)

    result = actions.stop_container("comfyui", reason="temperatura critica")

    assert result.dry_run is False
    assert result.success is True
    assert runner.calls == [["docker", "stop", "--time", "30", "comfyui"]]


def test_real_run_reports_failure_without_raising():
    runner = RecordingRunner(_fail(stderr="Error: No such container: comfyui"))
    actions = DockerActions(dry_run=False, command_runner=runner)

    result = actions.stop_container("comfyui", reason="temperatura critica")

    assert result.success is False
    assert result.error is not None


# --- monitor: contenedor inexistente / docker no disponible -----------


def test_get_status_for_nonexistent_container():
    runner = RecordingRunner(_fail(stderr="Error: No such object: ghost-container"))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("ghost-container")

    assert status.exists is False
    assert status.status == "not_found"


def test_is_available_false_when_docker_daemon_unreachable():
    runner = RecordingRunner(_fail(error="binario no encontrado: docker"))
    monitor = DockerMonitor(command_runner=runner)

    assert monitor.is_available() is False


def test_get_status_survives_malformed_json():
    runner = RecordingRunner(_ok(stdout="esto no es json"))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("comfyui")

    assert status.exists is False
    assert status.error is not None


# --- estado tri-valor de Docker: True / False / None -------------------


def test_running_is_true_when_docker_confirms_container_running():
    stdout = '[{"State": {"Status": "running"}, "RestartCount": 0}]'
    runner = RecordingRunner(_ok(stdout=stdout))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("comfyui")

    assert status.running is True
    assert status.status == "running"


def test_running_is_false_when_docker_confirms_container_stopped():
    stdout = '[{"State": {"Status": "exited"}, "RestartCount": 0}]'
    runner = RecordingRunner(_ok(stdout=stdout))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("comfyui")

    assert status.running is False
    assert status.status == "exited"


def test_running_is_false_when_container_does_not_exist():
    # Docker SI respondio y confirmo que no existe: es una confirmacion
    # valida de "no esta corriendo", distinta de "no se pudo consultar".
    runner = RecordingRunner(_fail(stderr="Error: No such object: ghost"))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("ghost")

    assert status.running is False
    assert status.status == "not_found"


def test_running_is_none_when_docker_cannot_be_queried():
    # El socket de Docker esta inaccesible o el comando fallo por un motivo
    # que no es "no existe": estado desconocido, NUNCA asumir detenido.
    runner = RecordingRunner(_fail(error="timeout tras 5.0s"))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("comfyui")

    assert status.running is None
    assert status.status == "unknown"


def test_running_is_none_when_inspect_output_is_malformed():
    runner = RecordingRunner(_ok(stdout="esto no es json"))
    monitor = DockerMonitor(command_runner=runner)

    status = monitor.get_status("comfyui")

    assert status.running is None
    assert status.status == "unknown"


# --- apagado: deshabilitado por defecto --------------------------------


def test_shutdown_is_blocked_when_not_explicitly_enabled():
    runner = RecordingRunner(_ok())
    actions = SystemActions(dry_run=False, shutdown_enabled=False, command_runner=runner)

    result = actions.safe_shutdown(reason="gpu_temperature_emergency")

    assert result.success is False
    assert "deshabilitado" in result.message
    assert runner.calls == []  # jamas se invoco el comando shutdown


def test_shutdown_blocked_even_in_dry_run_if_disabled_by_config():
    runner = RecordingRunner(_ok())
    actions = SystemActions(dry_run=True, shutdown_enabled=False, command_runner=runner)

    result = actions.safe_shutdown(reason="test")

    assert result.success is False
    assert runner.calls == []


def test_shutdown_dry_run_does_not_invoke_real_command_even_when_enabled():
    runner = RecordingRunner(_ok())
    actions = SystemActions(dry_run=True, shutdown_enabled=True, command_runner=runner)

    result = actions.safe_shutdown(reason="gpu_temperature_emergency")

    assert result.dry_run is True
    assert result.success is True
    assert runner.calls == []


def test_shutdown_real_run_invokes_shutdown_command_only_when_enabled_and_not_dry_run():
    runner = RecordingRunner(_ok())
    actions = SystemActions(dry_run=False, shutdown_enabled=True, command_runner=runner)

    result = actions.safe_shutdown(reason="gpu_temperature_emergency")

    assert result.success is True
    assert runner.calls
    assert runner.calls[0][:3] == ["shutdown", "-h", "+1"]
    assert "kill" not in " ".join(runner.calls[0]).lower()


def test_shutdown_denied_by_polkit_reports_failure_without_raising():
    # Documenta/verifica la politica fail-closed: si systemd-logind/polkit
    # rechaza el apagado (p.ej. porque hay otra sesion activa o un
    # inhibitor, ver README seccion "Shutdown"), el fallo se refleja en el
    # ActionResult -- nunca se lanza una excepcion ni se reintenta un
    # bypass por cuenta propia.
    runner = RecordingRunner(_fail(
        stderr="Failed to power off system via logind: Interactive authentication required.",
        returncode=1,
    ))
    actions = SystemActions(dry_run=False, shutdown_enabled=True, command_runner=runner)

    result = actions.safe_shutdown(reason="gpu_temperature_emergency")

    assert result.success is False
    assert result.dry_run is False
    assert "authentication" in (result.error or "").lower()
    assert runner.calls  # si se intento invocar el comando, solo que fallo
