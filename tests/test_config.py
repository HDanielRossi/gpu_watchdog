import textwrap

import pytest

from guardian.config import ConfigError, load_config, parse_config


VALID_MINIMAL = {
    "general": {"interval_seconds": 10, "dry_run": True},
    "logging": {"file": "/tmp/guardian-test.log"},
    "gpu": {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 78,
            "critical_temperature_c": 84,
            "emergency_temperature_c": 90,
        },
    },
    "docker": {"enabled": True, "monitored_containers": ["comfyui", "ollama", "open-webui"]},
    "services": {
        "comfyui": {"container_name": "comfyui", "health_url": "http://127.0.0.1:8188/"},
    },
}


def test_valid_minimal_config_parses_without_error():
    config = parse_config(VALID_MINIMAL)
    assert config.general.dry_run is True
    assert config.gpu.thresholds.critical_temperature_c == 84


def test_dry_run_defaults_to_true_when_omitted():
    raw = dict(VALID_MINIMAL)
    raw["general"] = {"interval_seconds": 10}
    config = parse_config(raw)
    assert config.general.dry_run is True


def test_root_must_be_a_mapping():
    with pytest.raises(ConfigError):
        parse_config([1, 2, 3])  # type: ignore[arg-type]


def test_non_numeric_interval_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["general"] = {"interval_seconds": "diez"}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("interval_seconds" in e for e in exc_info.value.errors)


def test_zero_interval_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["general"] = {"interval_seconds": 0}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("interval_seconds" in e for e in exc_info.value.errors)


def test_temperature_thresholds_out_of_order_are_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 90,
            "critical_temperature_c": 84,  # invertido: critical < warning
            "emergency_temperature_c": 95,
        },
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("warning < critical < emergency" in e for e in exc_info.value.errors)


def test_service_without_health_url_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["services"] = {"comfyui": {"container_name": "comfyui"}}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("health_url" in e for e in exc_info.value.errors)


def test_missing_config_file_raises_config_error(tmp_path):
    missing_path = tmp_path / "no-existe.yaml"
    with pytest.raises(ConfigError):
        load_config(str(missing_path))


def test_invalid_yaml_syntax_raises_config_error(tmp_path):
    bad_file = tmp_path / "config.yaml"
    bad_file.write_text("general:\n  dry_run: true\n\tinterval_seconds: 10\n")  # tab mezclado con espacios
    with pytest.raises(ConfigError):
        load_config(str(bad_file))


def test_telegram_enabled_without_env_vars_is_rejected(monkeypatch):
    monkeypatch.delenv("AI_GUARDIAN_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("AI_GUARDIAN_TELEGRAM_CHAT_ID", raising=False)
    raw = dict(VALID_MINIMAL)
    raw["notifications"] = {
        "enabled": True,
        "telegram": {"enabled": True, "bot_token_env": "AI_GUARDIAN_TELEGRAM_TOKEN", "chat_id_env": "AI_GUARDIAN_TELEGRAM_CHAT_ID"},
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("AI_GUARDIAN_TELEGRAM_TOKEN" in e for e in exc_info.value.errors)
    assert any("AI_GUARDIAN_TELEGRAM_CHAT_ID" in e for e in exc_info.value.errors)


def test_telegram_enabled_with_env_vars_present_parses_ok(monkeypatch):
    monkeypatch.setenv("AI_GUARDIAN_TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setenv("AI_GUARDIAN_TELEGRAM_CHAT_ID", "12345")
    raw = dict(VALID_MINIMAL)
    raw["notifications"] = {
        "enabled": True,
        "telegram": {"enabled": True, "bot_token_env": "AI_GUARDIAN_TELEGRAM_TOKEN", "chat_id_env": "AI_GUARDIAN_TELEGRAM_CHAT_ID"},
    }
    config = parse_config(raw)
    assert config.notifications.telegram.resolve_token() == "fake-token"
    assert config.notifications.telegram.resolve_chat_id() == "12345"


# --- validacion semantica (rangos) --------------------------------------


def test_negative_gpu_duration_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90,
            "warning_duration_seconds": -1,
        },
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("warning_duration_seconds" in e for e in exc_info.value.errors)


def test_negative_hysteresis_margin_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90,
            "hysteresis_margin_c": -5,
        },
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("hysteresis_margin_c" in e for e in exc_info.value.errors)


def test_negative_action_cooldown_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {"warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90},
        "actions": {"action_cooldown_seconds": -300},
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("action_cooldown_seconds" in e for e in exc_info.value.errors)


def test_vram_percent_out_of_range_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90,
            "maximum_vram_percent": 150,
        },
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("maximum_vram_percent" in e for e in exc_info.value.errors)


def test_negative_power_watts_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90,
            "maximum_power_watts": -10,
        },
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("maximum_power_watts" in e for e in exc_info.value.errors)


def test_negative_gpu_index_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "index": -1,
        "thresholds": {"warning_temperature_c": 78, "critical_temperature_c": 84, "emergency_temperature_c": 90},
    }
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("gpu.index" in e for e in exc_info.value.errors)


def test_negative_minimum_free_disk_gb_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["system"] = {"minimum_free_disk_gb": -20}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("minimum_free_disk_gb" in e for e in exc_info.value.errors)


def test_ram_percent_out_of_range_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["system"] = {"maximum_ram_percent": 101}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("maximum_ram_percent" in e for e in exc_info.value.errors)


def test_swap_percent_out_of_range_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["system"] = {"maximum_swap_percent": -1}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("maximum_swap_percent" in e for e in exc_info.value.errors)


def test_empty_disk_paths_list_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["system"] = {"disk_paths": []}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("disk_paths" in e for e in exc_info.value.errors)


def test_disk_paths_with_empty_string_entry_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["system"] = {"disk_paths": ["/", "  "]}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("disk_paths" in e for e in exc_info.value.errors)


def test_multiple_disk_paths_are_accepted():
    raw = dict(VALID_MINIMAL)
    raw["system"] = {"disk_paths": ["/", "/mnt/ai-storage"]}
    config = parse_config(raw)
    assert config.system.disk_paths == ["/", "/mnt/ai-storage"]


def test_negative_notifications_cooldown_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["notifications"] = {"enabled": False, "cooldown_seconds": -1}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("notifications.cooldown_seconds" in e for e in exc_info.value.errors)


def test_negative_telemetry_gap_tolerance_is_rejected():
    raw = dict(VALID_MINIMAL)
    raw["general"] = {"interval_seconds": 10, "telemetry_gap_tolerance_seconds": -5}
    with pytest.raises(ConfigError) as exc_info:
        parse_config(raw)
    assert any("telemetry_gap_tolerance_seconds" in e for e in exc_info.value.errors)


def test_telemetry_gap_tolerance_defaults_to_30_seconds():
    config = parse_config(VALID_MINIMAL)
    assert config.general.telemetry_gap_tolerance_seconds == 30.0


def test_valid_config_with_many_boundary_values_parses_ok():
    raw = dict(VALID_MINIMAL)
    raw["gpu"] = {
        "enabled": True,
        "thresholds": {
            "warning_temperature_c": 0, "critical_temperature_c": 1, "emergency_temperature_c": 150,
            "warning_duration_seconds": 0, "hysteresis_margin_c": 0,
            "maximum_vram_percent": 100, "maximum_power_watts": 0,
        },
        "actions": {"action_cooldown_seconds": 0},
    }
    raw["system"] = {
        "minimum_free_disk_gb": 0, "maximum_ram_percent": 100, "maximum_swap_percent": 0,
        "disk_paths": ["/"],
    }
    config = parse_config(raw)  # no debe lanzar: los limites en 0/100 son validos, no arbitrariamente estrictos
    assert config.gpu.thresholds.maximum_vram_percent == 100


def test_load_config_from_real_yaml_file(tmp_path):
    content = textwrap.dedent(
        """
        general:
          interval_seconds: 5
          dry_run: true
        logging:
          file: /tmp/guardian-test-2.log
        gpu:
          enabled: true
          thresholds:
            warning_temperature_c: 78
            critical_temperature_c: 84
            emergency_temperature_c: 90
        docker:
          enabled: true
          monitored_containers: [comfyui, ollama, open-webui]
        services:
          comfyui:
            container_name: comfyui
            health_url: http://127.0.0.1:8188/
        """
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content)

    config = load_config(str(config_path))

    assert config.general.interval_seconds == 5
    assert config.docker.monitored_containers == ["comfyui", "ollama", "open-webui"]
