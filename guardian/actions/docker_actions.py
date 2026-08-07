"""Acciones sobre contenedores Docker: detener y reiniciar.

Todas respetan dry_run, tienen timeout, registran el motivo y devuelven un
ActionResult estructurado. Una falla en una accion nunca lanza: se refleja
en el resultado para que el llamador decida que hacer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from guardian.models import ActionResult
from guardian.utils.commands import CommandResult, run_command

logger = logging.getLogger("ai_guardian.actions.docker")

CommandRunner = Callable[..., CommandResult]

_STOP_TIMEOUT_SECONDS = "30"


class DockerActions:
    def __init__(self, dry_run: bool, command_runner: CommandRunner = run_command, timeout: float = 45.0):
        self._dry_run = dry_run
        self._run = command_runner
        self._timeout = timeout

    def stop_container(self, name: str, reason: str) -> ActionResult:
        return self._execute("stop_container", ["docker", "stop", "--time", _STOP_TIMEOUT_SECONDS, name], name, reason)

    def restart_container(self, name: str, reason: str) -> ActionResult:
        return self._execute("restart_container", ["docker", "restart", "--time", _STOP_TIMEOUT_SECONDS, name], name, reason)

    def stop_containers(self, names: list[str], reason: str) -> list[ActionResult]:
        return [self.stop_container(name, reason) for name in names]

    def _execute(self, action_name: str, cmd: list[str], target: str, reason: str) -> ActionResult:
        now = datetime.now(timezone.utc)
        logger.info("action_requested action=%s target=%s reason=%s dry_run=%s", action_name, target, reason, self._dry_run)

        if self._dry_run:
            message = f"[dry_run] se habria ejecutado: {' '.join(cmd)}"
            logger.info("action_dry_run action=%s target=%s command=%s", action_name, target, " ".join(cmd))
            return ActionResult(action_name=action_name, target=target, success=True, dry_run=True, message=message, reason=reason, timestamp=now)

        result = self._run(cmd, self._timeout)
        if result.ok:
            logger.info("action_succeeded action=%s target=%s", action_name, target)
            return ActionResult(action_name=action_name, target=target, success=True, dry_run=False, message="ejecutado correctamente", reason=reason, timestamp=now)

        error = result.error or result.stderr.strip() or f"codigo de salida {result.returncode}"
        logger.error("action_failed action=%s target=%s error=%s", action_name, target, error)
        return ActionResult(action_name=action_name, target=target, success=False, dry_run=False, message="fallo la ejecucion", reason=reason, timestamp=now, error=error)
