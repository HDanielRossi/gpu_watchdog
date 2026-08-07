"""Configuracion de logging con rotacion para AI Guardian."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from guardian.config import LoggingConfig

LOGGER_NAME = "ai_guardian"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(config: "LoggingConfig", *, also_console: bool = True) -> logging.Logger:
    """Configura y devuelve el logger raiz de AI Guardian.

    Nunca lanza si no puede crear el archivo de log (por ejemplo, permisos
    insuficientes en /var/log antes de correr como usuario dedicado): en ese
    caso degrada a solo consola para no impedir que `ai-guardian` arranque.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(config.level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    try:
        log_path = Path(config.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        also_console = True
        sys.stderr.write(f"ai-guardian: no se pudo abrir el archivo de log ({exc}); usando solo consola\n")

    if also_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Registra una linea estructurada tipo `event key=value key2=value2`.

    Nunca incluir aqui tokens, contrasenas ni secretos.
    """
    if fields:
        rendered = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.log(level, "%s %s", event, rendered)
    else:
        logger.log(level, "%s", event)
