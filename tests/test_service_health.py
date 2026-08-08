"""Tests de ServiceHealthChecker, en particular el manejo tri-valor de
`container_running` (True/False/None) y su interaccion con el HTTP check.

Todos los checks HTTP se mockean reemplazando `urllib.request.urlopen`; no
se hace ninguna llamada de red real.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from guardian.models import ServiceStatus
from guardian.monitors.services import ServiceHealthChecker


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def test_container_confirmed_stopped_skips_http_call(monkeypatch):
    called = False

    def _fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("no deberia llamarse: el contenedor esta confirmado como detenido")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    checker = ServiceHealthChecker()

    check = checker.check("ollama", "http://127.0.0.1:11434/api/tags", container_running=False)

    assert check.status == ServiceStatus.STOPPED
    assert called is False


def test_container_confirmed_running_attempts_http_and_reports_healthy(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(200))
    checker = ServiceHealthChecker()

    check = checker.check("ollama", "http://127.0.0.1:11434/api/tags", container_running=True)

    assert check.status == ServiceStatus.HEALTHY
    assert check.http_status == 200


def test_docker_unknown_still_attempts_http_and_reports_healthy(monkeypatch):
    # Caso central del requisito: socket de Docker inaccesible (running=None)
    # pero el servicio SI responde por HTTP -> debe considerarse accesible.
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(200))
    checker = ServiceHealthChecker()

    check = checker.check("ollama", "http://127.0.0.1:11434/api/tags", container_running=None)

    assert check.status == ServiceStatus.HEALTHY
    assert check.http_status == 200


def test_docker_unknown_and_http_unreachable_reports_container_up_no_http(monkeypatch):
    def _raise(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    checker = ServiceHealthChecker()

    check = checker.check("ollama", "http://127.0.0.1:11434/api/tags", container_running=None)

    # Docker desconocido + HTTP tambien falla: no hay evidencia de que este
    # arriba, pero tampoco se reporto "stopped" (eso requeriria confirmacion
    # explicita de Docker, que aqui no existe).
    assert check.status == ServiceStatus.CONTAINER_UP_NO_HTTP
    assert check.status != ServiceStatus.STOPPED


def test_docker_unknown_logs_distinction_from_confirmed_stopped(monkeypatch, caplog):
    import logging
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(200))
    checker = ServiceHealthChecker()

    with caplog.at_level(logging.INFO, logger="ai_guardian.monitors.services"):
        checker.check("ollama", "http://127.0.0.1:11434/api/tags", container_running=None)

    assert any("service_docker_status_unknown" in record.message for record in caplog.records)
