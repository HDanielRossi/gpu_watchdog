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
    # Nota: esto solo vale dentro de `telemetry_gap_tolerance_seconds`
    # (default 30s aqui, ver WatcherConfig); un hueco mas largo si reinicia
    # el progreso -- eso se cubre en la seccion "tolerancia a huecos de
    # telemetria" mas abajo.
    watcher = ThresholdWatcher(_config(duration_seconds=60.0))
    watcher.evaluate(90.0, START)

    missing = watcher.evaluate(None, START + timedelta(seconds=1))
    assert missing.newly_triggered is False
    assert missing.active is False

    # Hueco corto (5s, dentro de la tolerancia): el progreso hacia la
    # duracion no se perdio por la lectura faltante.
    watcher.evaluate(90.0, START + timedelta(seconds=1 + 5))
    resumed = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert resumed.newly_triggered is True


# --- tolerancia a huecos de telemetria ----------------------------------
#
# Convencion usada en estos tests: el "hueco" empieza a contar desde la
# PRIMERA lectura None (se marca `first_missing_at` en ese instante), y su
# duracion se mide hasta la siguiente llamada a evaluate() -- sea otra
# lectura None o la primera lectura real de recuperacion. Por eso cada test
# fija el None en t=1 (un ciclo despues del ultimo dato bueno en t=0) y la
# recuperacion en t=1+gap, para que la duracion medida sea exactamente
# `gap` segundos.


def test_short_telemetry_gap_5s_preserves_sustained_progress():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)  # arranca el periodo sostenido en t=0
    watcher.evaluate(None, START + timedelta(seconds=1))  # el hueco empieza en t=1
    watcher.evaluate(90.0, START + timedelta(seconds=1 + 5))  # recupera 5s despues: gap=5s

    # El anchor original (t=0) sigue vigente: a los 61s totales dispara.
    resumed = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert resumed.newly_triggered is True


def test_telemetry_gap_29s_within_tolerance_preserves_progress():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))
    watcher.evaluate(90.0, START + timedelta(seconds=1 + 29))  # gap=29s

    resumed = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert resumed.newly_triggered is True


def test_telemetry_gap_exactly_at_tolerance_boundary_preserves_progress():
    # gap = 30s, tolerancia = 30s: dentro del limite (no supera la tolerancia).
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))
    watcher.evaluate(90.0, START + timedelta(seconds=1 + 30))  # gap=30s exactos

    resumed = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert resumed.newly_triggered is True


def test_telemetry_gap_just_above_tolerance_resets_progress():
    # gap = 31s > tolerancia 30s: se reinicia el progreso sostenido.
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))
    recovery_time = START + timedelta(seconds=1 + 31)
    recovery = watcher.evaluate(90.0, recovery_time)  # gap=31s -> reinicia, nuevo anchor aqui mismo
    assert recovery.newly_triggered is False  # el anchor nuevo recien empieza (elapsed=0)

    # El anchor original (t=0) ya no cuenta: a los 61s totales desde el
    # inicio original NO deberia dispararse (solo ~30s reales desde el reset).
    not_yet = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert not_yet.newly_triggered is False

    # Pero 60s despues del nuevo anchor (en recovery_time) si dispara.
    triggered = watcher.evaluate(90.0, recovery_time + timedelta(seconds=61))
    assert triggered.newly_triggered is True


def test_prolonged_telemetry_loss_resets_progress():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))
    recovery_time = START + timedelta(seconds=1 + 300)  # perdida prolongada: gap=300s
    recovery = watcher.evaluate(90.0, recovery_time)
    assert recovery.newly_triggered is False  # nuevo anchor, no hereda el progreso previo

    triggered = watcher.evaluate(90.0, recovery_time + timedelta(seconds=61))
    assert triggered.newly_triggered is True


def test_hot_reading_after_reset_starts_a_fresh_sustained_period():
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))
    recovery_time = START + timedelta(seconds=1 + 100)  # gap=100s > tolerancia

    first_after_reset = watcher.evaluate(90.0, recovery_time)
    assert first_after_reset.newly_triggered is False  # arranca un periodo nuevo desde cero

    triggered = watcher.evaluate(90.0, recovery_time + timedelta(seconds=61))
    assert triggered.newly_triggered is True


def test_normal_recovery_below_threshold_is_unaffected_by_gap_tolerance():
    # Una lectura real por debajo del umbral sigue reiniciando el progreso
    # exactamente igual que antes de introducir la tolerancia de telemetria
    # (es un mecanismo aparte: histeresis/threshold, no huecos de datos).
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))  # hueco corto, dentro de tolerancia
    recovered_reading = watcher.evaluate(70.0, START + timedelta(seconds=1 + 5))
    assert recovered_reading.active is False

    watcher.evaluate(90.0, START + timedelta(seconds=10))  # nuevo anchor
    not_yet = watcher.evaluate(90.0, START + timedelta(seconds=10 + 59))
    assert not_yet.newly_triggered is False


def test_multiple_consecutive_none_readings_measure_cumulative_gap():
    # El hueco se mide de forma acumulada desde la primera lectura None, no
    # se reinicia con cada nueva lectura None individual.
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))  # el hueco empieza en t=1
    watcher.evaluate(None, START + timedelta(seconds=6))  # +5s acumulados
    recovery = watcher.evaluate(90.0, START + timedelta(seconds=1 + 25))  # +24s desde t=1: 25s totales, <=30

    assert recovery.newly_triggered is False  # aun no pasaron los 60s del anchor original (t=0)
    triggered = watcher.evaluate(90.0, START + timedelta(seconds=61))
    assert triggered.newly_triggered is True  # anchor original preservado: dispara a los 61s

    watcher2 = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher2.evaluate(90.0, START)
    watcher2.evaluate(None, START + timedelta(seconds=1))  # hueco empieza en t=1
    watcher2.evaluate(None, START + timedelta(seconds=6))  # +5s acumulados, aun dentro de tolerancia
    recovery2_time = START + timedelta(seconds=1 + 35)  # +34s desde t=1: 35s totales, supera 30s
    recovery2 = watcher2.evaluate(90.0, recovery2_time)
    assert recovery2.newly_triggered is False  # nuevo anchor (elapsed=0)

    not_yet = watcher2.evaluate(90.0, START + timedelta(seconds=61))
    assert not_yet.newly_triggered is False  # el anchor original ya no cuenta
    triggered2 = watcher2.evaluate(90.0, recovery2_time + timedelta(seconds=61))
    assert triggered2.newly_triggered is True  # el anchor nuevo si dispara a sus 60s


def test_telemetry_gap_state_round_trips_through_state_dict():
    # Escenario clave: el daemon se reinicia, carga un estado persistido con
    # un hueco de telemetria ya en curso, y la primera lectura tras el
    # reinicio es una lectura real caliente. El reinicio del progreso debe
    # aplicarse igual que si el proceso jamas se hubiera reiniciado.
    watcher = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    watcher.evaluate(90.0, START)
    watcher.evaluate(None, START + timedelta(seconds=1))  # hueco empieza en t=1

    state = watcher.to_state_dict()
    assert state["first_missing_at"] is not None
    assert state["active"] is False

    restored = ThresholdWatcher(_config(duration_seconds=60.0, telemetry_gap_tolerance_seconds=30.0))
    restored.load_state_dict(state)

    # Recupera 35s despues del inicio del hueco (t=1+35=36): supera la
    # tolerancia de 30s, asi que debe reiniciar el progreso pese a venir de
    # un estado restaurado.
    recovery = restored.evaluate(90.0, START + timedelta(seconds=1 + 35))
    assert recovery.newly_triggered is False  # nuevo anchor, no dispara de inmediato


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
