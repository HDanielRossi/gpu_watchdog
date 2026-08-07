"""Accion de apagado seguro del sistema.

Deshabilitada por defecto. Requiere que `shutdown_on_emergency` este
explicitamente en `true` en la configuracion ademas de que dry_run sea
`false`; ninguna de las dos condiciones por si sola alcanza. Nunca usa
`kill -9` ni un apagado inmediato: usa `shutdown -h +1` (un minuto de
margen) para dar tiempo a que las cargas de IA, detenidas previamente por
las acciones de Docker, terminen de liberar la GPU.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from guardian.models import ActionResult
from guardian.utils.commands import CommandResult, run_command

logger = logging.getLogger("ai_guardian.actions.system")

CommandRunner = Callable[..., CommandResult]


class SystemActions:
    def __init__(self, dry_run: bool, shutdown_enabled: bool, command_runner: CommandRunner = run_command, timeout: float = 30.0):
        self._dry_run = dry_run
        self._shutdown_enabled = shutdown_enabled
        self._run = command_runner
        self._timeout = timeout

    def safe_shutdown(self, reason: str) -> ActionResult:
        action_name = "safe_shutdown"
        now = datetime.now(timezone.utc)
        logger.warning(
            "action_requested action=%s reason=%s dry_run=%s shutdown_enabled=%s",
            action_name, reason, self._dry_run, self._shutdown_enabled,
        )

        if not self._shutdown_enabled:
            message = "apagado no ejecutado: 'gpu.actions.shutdown_on_emergency' esta deshabilitado en la configuracion"
            logger.warning("action_blocked action=%s reason=%s", action_name, message)
            return ActionResult(action_name=action_name, target="system", success=False, dry_run=self._dry_run, message=message, reason=reason, timestamp=now)

        cmd = ["shutdown", "-h", "+1", f"AI Guardian: {reason}"]
        if self._dry_run:
            message = f"[dry_run] se habria ejecutado: {' '.join(cmd)}"
            logger.warning("action_dry_run action=%s command=%s", action_name, " ".join(cmd))
            return ActionResult(action_name=action_name, target="system", success=True, dry_run=True, message=message, reason=reason, timestamp=now)

        result = self._run(cmd, self._timeout)
        if result.ok:
            logger.critical("action_succeeded action=%s reason=%s", action_name, reason)
            return ActionResult(action_name=action_name, target="system", success=True, dry_run=False, message="apagado programado", reason=reason, timestamp=now)

        error = result.error or result.stderr.strip() or f"codigo de salida {result.returncode}"
        logger.error("action_failed action=%s error=%s", action_name, error)
        return ActionResult(action_name=action_name, target="system", success=False, dry_run=False, message="fallo la ejecucion", reason=reason, timestamp=now, error=error)
