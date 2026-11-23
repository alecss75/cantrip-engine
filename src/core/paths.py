"""
paths.py - Rutas canónicas independientes del directorio de trabajo (CWD).

El proyecto distingue dos raíces deliberadamente:

* ``RESOURCE_ROOT`` (solo lectura): recursos estáticos empaquetados. En modo
  código fuente es la raíz del repositorio; en un binario PyInstaller es
  ``sys._MEIPASS``. Coincide con ``fs_utils.get_bundle_dir()``.
* ``DATA_ROOT`` (lectura/escritura): datos del jugador. Coincide con
  ``fs_utils.get_user_data_dir()``: raíz del proyecto en fuente, carpeta de
  datos del sistema operativo en binario y carpeta del ejecutable en modo
  portable.

Ninguna ruta de datos debe resolverse contra ``os.getcwd()``. El código nuevo
debe usar las funciones ``get_*_dir()``; las constantes ``SAVES_DIR``,
``DATA_DIR`` y ``LOGS_DIR`` existen por compatibilidad con TICKET-01 y se
resuelven dinámicamente mediante ``__getattr__`` para que un cambio de raíz
(tests, modo portable, binario) se refleje sin reimportar el módulo.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("cantrip.paths")


def _resource_root() -> Path:
    """Raíz de recursos de solo lectura (misma semántica que get_bundle_dir)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent.parent.parent


#: Raíz de recursos estáticos. No escribir aquí en builds empaquetadas.
RESOURCE_ROOT = _resource_root()

#: Alias retrocompatible del nombre usado por TICKET-01. En fuente apunta al
#: repositorio; en binario apunta a ``_MEIPASS`` (no a la carpeta de datos).
PROJECT_ROOT = RESOURCE_ROOT

#: Alias explícito de la raíz de recursos (TICKET-06).
BUNDLE_DIR = RESOURCE_ROOT

SRC_DIR = RESOURCE_ROOT / "src"
DOCS_DIR = RESOURCE_ROOT / "documentacion"


def get_bundle_dir() -> Path:
    """Raíz de recursos de solo lectura (compatibilidad con ``fs_utils``)."""
    return RESOURCE_ROOT


def get_data_root() -> Path:
    """Raíz escribible de los datos del jugador (nunca depende del CWD)."""
    from src.storage.fs_utils import get_user_data_dir

    return Path(get_user_data_dir()).resolve()


def get_saves_dir() -> Path:
    """Carpeta de partidas guardadas."""
    return get_data_root() / "saves"


def get_data_dir() -> Path:
    """Carpeta de datos generados (índices de NPCs, estado legado, etc.)."""
    return get_data_root() / "data"


def get_logs_dir() -> Path:
    """Carpeta de logs de la aplicación."""
    return get_data_root() / "logs"


def get_exports_dir() -> Path:
    """Carpeta de exportaciones (PDF, paquetes de feedback, etc.)."""
    return get_data_root() / "exports"


def get_config_dir() -> Path:
    """Carpeta de configuración persistente de la interfaz."""
    return get_saves_dir() / "config"


def resolve_data_path(path: str | Path) -> Path:
    """Resuelve una ruta de datos contra ``DATA_ROOT``, no contra el CWD.

    Las rutas absolutas se respetan tal cual; las relativas (por ejemplo
    ``"saves/mi_campana/gamestate.json"`` o ``"data/npc_index.json"``) se
    anclan a la raíz de datos del jugador.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = get_data_root() / candidate
    return candidate.resolve()


def bind_campaign_state(estado: dict, campaign_folder: str | Path) -> dict:
    """Fija ``estado["meta"]["carpeta"]`` a la carpeta real de la campaña.

    Los saves antiguos guardaron rutas relativas (``saves\\Mi Campaña``); si la
    partida se carga desde otro CWD, esas rutas apuntarían a un lugar
    equivocado. Al normalizarlas al cargar, los consumidores de ``meta.carpeta``
    (NPCs, mapa, archivo de logs) siguen trabajando sobre la carpeta correcta y
    el siguiente guardado sanea el JSON en disco.
    """
    if not isinstance(estado, dict):
        return estado
    meta = estado.get("meta")
    if not isinstance(meta, dict):
        return estado

    folder = resolve_data_path(campaign_folder)
    if folder.name == "gamestate.json":
        folder = folder.parent
    meta["carpeta"] = str(folder)
    return estado


def ensure_data_dirs() -> Path:
    """Crea las carpetas escribibles base y devuelve la raíz de datos.

    Es tolerante a errores: un disco de solo lectura no debe tumbar el arranque.
    """
    root = get_data_root()
    for directory in (
        root / "saves",
        root / "data",
        root / "logs",
        root / "exports",
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("No se pudo crear %s: %s", directory, exc)
    return root


#: Constantes de TICKET-01 resueltas bajo demanda (ver ``__getattr__``).
_DYNAMIC_DIRS = {
    "SAVES_DIR": get_saves_dir,
    "DATA_DIR": get_data_dir,
    "LOGS_DIR": get_logs_dir,
    "EXPORTS_DIR": get_exports_dir,
    "USER_DATA_DIR": get_data_root,
}


def __getattr__(name: str):
    factory = _DYNAMIC_DIRS.get(name)
    if factory is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return factory()


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_DYNAMIC_DIRS))
