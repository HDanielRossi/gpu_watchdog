"""Wrapper unico y seguro para invocar comandos externos via subprocess.

Centraliza el manejo de binarios ausentes, timeouts y errores inesperados
para que ningun monitor o accion pueda tumbar el daemon por una falla aqui.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.returncode == 0


def run_command(
    args: Sequence[str],
    timeout: float = 5.0,
    input_text: Optional[str] = None,
) -> CommandResult:
    """Ejecuta un comando externo sin nunca lanzar una excepcion.

    Cualquier fallo (binario ausente, timeout, error inesperado) se traduce
    en un CommandResult con `error` poblado en lugar de propagar la excepcion.
    """
    args_tuple = tuple(args)
    try:
        proc = subprocess.run(
            args_tuple,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(
            args=args_tuple,
            returncode=None,
            stdout="",
            stderr="",
            timed_out=False,
            error=f"binario no encontrado: {args_tuple[0] if args_tuple else '?'}",
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            args=args_tuple,
            returncode=None,
            stdout="",
            stderr="",
            timed_out=True,
            error=f"timeout tras {timeout}s",
        )
    except OSError as exc:
        return CommandResult(
            args=args_tuple,
            returncode=None,
            stdout="",
            stderr="",
            timed_out=False,
            error=f"error de sistema operativo: {exc}",
        )

    return CommandResult(
        args=args_tuple,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timed_out=False,
        error=None,
    )
