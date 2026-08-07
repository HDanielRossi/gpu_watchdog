"""Persistencia ligera del ultimo estado conocido en un archivo JSON.

No se usa base de datos: alcanza con un unico archivo que se sobreescribe de
forma atomica en cada ciclo, y que permite que el daemon se recupere de un
reinicio sin perder el progreso de duracion/cooldown de las reglas.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai_guardian.state")


class StateStore:
    def __init__(self, path: str):
        self._path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("state_load_failed path=%s error=%s", self._path, exc)
            return {}

    def save(self, data: dict[str, Any]) -> bool:
        """Escritura atomica (archivo temporal + rename) para no dejar el
        estado corrupto si el proceso se interrumpe a mitad de escritura."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, prefix=".guardian-state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, default=_json_default)
                os.replace(tmp_path, self._path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            return True
        except OSError as exc:
            logger.warning("state_save_failed path=%s error=%s", self._path, exc)
            return False


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"objeto no serializable: {type(obj).__name__}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
