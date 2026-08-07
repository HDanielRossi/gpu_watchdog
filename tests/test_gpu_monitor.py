from guardian.monitors.gpu import GpuMonitor
from guardian.utils.commands import CommandResult


def _ok(stdout: str) -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="", timed_out=False, error=None)


def _fail(error: str) -> CommandResult:
    return CommandResult(args=(), returncode=None, stdout="", stderr="", timed_out=False, error=error)


def _timeout() -> CommandResult:
    return CommandResult(args=(), returncode=None, stdout="", stderr="", timed_out=True, error="timeout tras 5.0s")


class FakeRunner:
    """Simula run_command devolviendo respuestas distintas por comando."""

    def __init__(self, query_result: CommandResult, throttle_result: CommandResult):
        self.query_result = query_result
        self.throttle_result = throttle_result
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=5.0, input_text=None):
        self.calls.append(list(args))
        if "clocks_throttle_reasons.hw_slowdown" in args[-2]:
            return self.throttle_result
        return self.query_result


def test_parses_well_formed_csv_line():
    query = _ok("0, NVIDIA GeForce RTX 3090, 52, 10, 23051, 24576, 28.59, 420.00, 30, P8\n")
    throttle = _ok("Not Active, Not Active, Not Active, Not Active, Not Active\n")
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    samples = monitor.read_all()

    assert len(samples) == 1
    sample = samples[0]
    assert sample.index == 0
    assert sample.name == "NVIDIA GeForce RTX 3090"
    assert sample.temperature_c == 52
    assert sample.utilization_percent == 10
    assert sample.memory_used_mb == 23051
    assert sample.memory_total_mb == 24576
    assert round(sample.memory_percent, 1) == round(23051 / 24576 * 100, 1)
    assert sample.power_draw_w == 28.59
    assert sample.power_limit_w == 420.00
    assert sample.fan_speed_percent == 30
    assert sample.pstate == "P8"
    assert sample.available is True
    assert sample.throttle_active is False


def test_handles_na_values_without_raising():
    # GPU sin ventilador reportado (portatiles / algunas cargas de datacenter)
    # y potencia no soportada: ambos casos deben quedar en None, no lanzar.
    query = _ok("0, NVIDIA T4, 45, 5, 1000, 16384, [N/A], [N/A], [N/A], P0\n")
    throttle = _ok("Not Active, Not Active, Not Active, Not Active, Not Active\n")
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    samples = monitor.read_all()

    assert len(samples) == 1
    sample = samples[0]
    assert sample.power_draw_w is None
    assert sample.power_limit_w is None
    assert sample.fan_speed_percent is None
    assert sample.temperature_c == 45


def test_missing_nvidia_driver_returns_empty_list_without_raising():
    query = _fail("binario no encontrado: nvidia-smi")
    throttle = _fail("binario no encontrado: nvidia-smi")
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    samples = monitor.read_all()

    assert samples == []


def test_command_timeout_returns_empty_list_without_raising():
    query = _timeout()
    throttle = _timeout()
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    samples = monitor.read_all()

    assert samples == []


def test_throttle_reasons_are_parsed_when_active():
    query = _ok("0, NVIDIA GeForce RTX 3090, 88, 99, 24000, 24576, 400, 420, 80, P2\n")
    throttle = _ok("Active, Active, Not Active, Not Active, Not Active\n")
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    samples = monitor.read_all()

    assert samples[0].throttle_active is True
    assert samples[0].throttle_reasons == ["hw_slowdown", "hw_thermal_slowdown"]


def test_malformed_line_is_skipped_not_raised():
    query = _ok("esto,no,es,una,linea,valida\n")
    throttle = _ok("Not Active, Not Active, Not Active, Not Active, Not Active\n")
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    samples = monitor.read_all()

    assert samples == []


def test_read_filters_by_index():
    query = _ok(
        "0, GPU0, 50, 1, 100, 24576, 10, 350, 20, P8\n"
        "1, GPU1, 60, 2, 200, 24576, 20, 350, 25, P8\n"
    )
    throttle = _ok("Not Active, Not Active, Not Active, Not Active, Not Active\n")
    monitor = GpuMonitor(command_runner=FakeRunner(query, throttle))

    sample = monitor.read(1)

    assert sample is not None
    assert sample.index == 1
    assert sample.name == "GPU1"
