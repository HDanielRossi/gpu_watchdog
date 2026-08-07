from datetime import datetime, timedelta, timezone

from guardian.rules.engine import ThresholdWatcher, WatcherConfig
from guardian.models import Severity

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config(**overrides) -> WatcherConfig:
    defaults = dict(
        name="gpu_temperature_critical",
        metric="gpu_temperature_c",
        severity=Severity.CRITICAL,
        threshold=84.0,
        duration_seconds=60.0,
        hysteresis_margin=5.0,
        cooldown_seconds=300.0,
        comparison="max",
    )
    defaults.update(overrides)
    return WatcherConfig(**defaults)


def test_does_not_trigger_on_a_single_isolated_reading_above_threshold():
    watcher = ThresholdWatcher(_config())

    event = watcher.evaluate(90.0, START)

    assert event.newly_triggered is False
    assert event.active is False


def test_triggers_only_after_sustained_duration_above_threshold():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0))

    e1 = watcher.evaluate(90.0, START)
    e2 = watcher.evaluate(90.0, START + timedelta(seconds=30))
    assert e1.newly_triggered is False
    assert e2.newly_triggered is False  # aun no se cumplen los 60s

    e3 = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert e3.newly_triggered is True
    assert e3.active is True
    assert e3.can_act is True


def test_dip_below_threshold_before_duration_resets_the_timer():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0))

    watcher.evaluate(90.0, START)
    watcher.evaluate(80.0, START + timedelta(seconds=30))  # baja antes de cumplir la duracion
    event = watcher.evaluate(90.0, START + timedelta(seconds=61))

    # El timer se reinicio en el segundo 30, asi que a los 61s totales
    # (solo 31s desde el reinicio) todavia no deberia activarse.
    assert event.newly_triggered is False


def test_hysteresis_keeps_alarm_active_until_value_drops_below_recovery_point():
    # umbral 84, margen 5 => se recupera solo por debajo de 79.
    watcher = ThresholdWatcher(_config(threshold=84.0, hysteresis_margin=5.0, duration_seconds=10.0))
    watcher.evaluate(90.0, START)
    triggered = watcher.evaluate(90.0, START + timedelta(seconds=11))
    assert triggered.active is True

    still_active = watcher.evaluate(80.0, START + timedelta(seconds=20))
    assert still_active.active is True
    assert still_active.recovered is False

    recovered = watcher.evaluate(78.9, START + timedelta(seconds=30))
    assert recovered.active is False
    assert recovered.recovered is True


def test_cooldown_prevents_repeated_action_while_condition_persists():
    watcher = ThresholdWatcher(_config(threshold=84.0, hysteresis_margin=5.0, duration_seconds=10.0, cooldown_seconds=300.0))
    watcher.evaluate(90.0, START)
    first = watcher.evaluate(90.0, START + timedelta(seconds=11))
    assert first.newly_triggered is True
    assert first.can_act is True

    # Se recupera y vuelve a dispararse rapidamente, dentro del cooldown.
    watcher.evaluate(70.0, START + timedelta(seconds=20))
    watcher.evaluate(90.0, START + timedelta(seconds=21))
    second = watcher.evaluate(90.0, START + timedelta(seconds=32))
    assert second.newly_triggered is True
    assert second.can_act is False  # cooldown de 300s aun no expira

    watcher.evaluate(70.0, START + timedelta(seconds=40))  # recupera de nuevo
    fourth_trigger_time = START + timedelta(seconds=350)
    watcher.evaluate(90.0, fourth_trigger_time)
    fourth = watcher.evaluate(90.0, fourth_trigger_time + timedelta(seconds=11))
    assert fourth.newly_triggered is True
    assert fourth.can_act is True  # ya paso el cooldown desde el primer disparo


def test_min_comparison_used_for_inverse_metrics_like_free_disk_space():
    watcher = ThresholdWatcher(_config(
        name="system_disk_low", metric="disk_free_gb", threshold=20.0,
        hysteresis_margin=5.0, duration_seconds=10.0, comparison="min",
    ))

    watcher.evaluate(10.0, START)  # 10GB libres < 20GB umbral -> por debajo
    triggered = watcher.evaluate(10.0, START + timedelta(seconds=11))
    assert triggered.newly_triggered is True

    # Se recupera solo por encima de 20 + 5 = 25 GB libres.
    not_yet = watcher.evaluate(22.0, START + timedelta(seconds=12))
    assert not_yet.recovered is False

    recovered = watcher.evaluate(26.0, START + timedelta(seconds=13))
    assert recovered.recovered is True


def test_missing_reading_does_not_reset_progress_nor_trigger():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0))
    watcher.evaluate(90.0, START)

    missing = watcher.evaluate(None, START + timedelta(seconds=30))
    assert missing.newly_triggered is False
    assert missing.active is False

    # El progreso hacia la duracion no se perdio por la lectura faltante.
    resumed = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert resumed.newly_triggered is True


def test_state_round_trip_preserves_active_and_cooldown():
    watcher = ThresholdWatcher(_config(duration_seconds=10.0, cooldown_seconds=300.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(90.0, START + timedelta(seconds=11))

    state = watcher.to_state_dict()
    assert state["active"] is True

    restored = ThresholdWatcher(_config(duration_seconds=10.0, cooldown_seconds=300.0))
    restored.load_state_dict(state)

    # Un disparo inmediato tras restaurar debe seguir en cooldown.
    event = restored.evaluate(90.0, START + timedelta(seconds=20))
    assert event.active is True
