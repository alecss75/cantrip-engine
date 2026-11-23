import os
import re
import sys
import json
import unicodedata
from pathlib import Path
from typing import Dict, Any

APP_FOLDER_NAME = "Cantrip-Engine"
PORTABLE_FLAG_NAME = "portable.flag"

#: Rutas de datos de jugador que se migran desde la carpeta de instalación
#: cuando el jugador viene de una build antigua «portable».
RUTAS_DATOS_JUGADOR = (
    "saves",
    "logs",
    "exports",
    "data/npcs",
    "data/npc_index.json",
    ".env",
)


def is_frozen() -> bool:
    """Retorna True si la aplicación se está ejecutando como binario compilado (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def get_install_dir() -> Path | None:
    """
    Devuelve la carpeta de instalación del ejecutable.

    - Build empaquetada: directorio que contiene el `.exe` / binario.
    - Modo código fuente: `None` (no hay «instalación» que actualizar).

    Es la raíz que un actualizador puede reemplazar por completo, así que nunca
    debe contener datos de jugador (ver `get_user_data_dir`).
    """
    if not is_frozen():
        return None
    try:
        return Path(sys.executable).resolve().parent
    except Exception:
        return None


def is_portable() -> bool:
    """
    True si el jugador activó el modo portable con un archivo `portable.flag`
    junto al ejecutable (pensado para USB: los datos viajan con la app).

    El modo portable desactiva la actualización in-app, porque el intercambio de
    carpeta reemplazaría también las partidas. En macOS no aplica: el `.app` vive
    en rutas de solo lectura y los datos siempre van a Application Support.
    """
    if not is_frozen() or sys.platform == "darwin":
        return False
    install_dir = get_install_dir()
    if install_dir is None:
        return False
    try:
        return (install_dir / PORTABLE_FLAG_NAME).exists()
    except OSError:
        return False


def get_migration_marker_path() -> Path:
    """Ruta del marcador que deja constancia de una migración de datos ya hecha."""
    return get_user_data_dir() / "MIGRATED_FROM_INSTALL.txt"


def get_bundle_dir() -> Path:
    """
    Devuelve la ruta base de los recursos estáticos empaquetados (solo lectura).
    - En ejecutable PyInstaller: `Path(sys._MEIPASS)`
    - En modo código fuente: raíz del repositorio.
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent.parent.parent


def get_user_data_dir() -> Path:
    """
    Devuelve el directorio persistente de lectura/escritura para datos del jugador
    (`saves/`, `logs/`, `exports/`, `data/npcs/`).

    - En modo código fuente: la raíz del proyecto.
    - En build empaquetada: el directorio de datos del sistema operativo, **fuera**
      del árbol de instalación, para que una actualización (que reemplaza la carpeta
      completa) nunca pueda borrar partidas:
        * Windows: `%LOCALAPPDATA%\\Cantrip-Engine`
        * macOS:   `~/Library/Application Support/Cantrip-Engine`
        * Linux:   `$XDG_DATA_HOME/Cantrip-Engine` (o `~/.local/share/Cantrip-Engine`)
    - Con `portable.flag` junto al ejecutable (Windows/Linux): la carpeta del `.exe`.
    """
    if not is_frozen():
        return Path(__file__).resolve().parent.parent.parent

    # En macOS, un .app bundle nunca debe escribir dentro de Contents/MacOS
    if sys.platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support" / APP_FOLDER_NAME
    elif sys.platform == "win32":
        appdata = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
        base_dir = Path(appdata) / APP_FOLDER_NAME
    else:
        xdg_data = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base_dir = Path(xdg_data) / APP_FOLDER_NAME

    if is_portable():
        install_dir = get_install_dir()
        if install_dir is not None:
            base_dir = install_dir

    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir
    except OSError:
        # Último recurso: carpeta junto al ejecutable (instalaciones muy restringidas).
        install_dir = get_install_dir()
        if install_dir is not None:
            try:
                install_dir.mkdir(parents=True, exist_ok=True)
                return install_dir
            except OSError:
                pass
        return Path.home()


def ensure_working_dir() -> Path:
    """
    Garantiza que el directorio de trabajo actual (`os.getcwd()`) apunte a `get_user_data_dir()`
    cuando se ejecuta desde un binario compilado o acceso directo externo.
    """
    data_dir = get_user_data_dir()
    if is_frozen():
        try:
            os.chdir(data_dir)
        except OSError:
            pass
    return data_dir


def get_build_info() -> Dict[str, Any]:
    """
    Lee el archivo `BUILD_INFO.json` generado durante el pipeline de CI/CD (GitHub Actions -> Itch.io)
    para mostrar la versión exacta y el hash del commit auditado en la interfaz.
    """
    from src.version import (
        __version__,
        APP_NAME,
        PUBLIC_REPO,
        PUBLIC_REPO_URL,
        ITCH_URL,
        KOFI_URL,
        SUPPORT_EMAIL,
        clean_version,
    )

    default_info = {
        "app_name": APP_NAME,
        "version": __version__,
        "channel": "source-dev" if not is_frozen() else "itch-release",
        "git_commit": "local",
        "build_date": "desarrollo",
        "repository": PUBLIC_REPO,
        "repository_url": PUBLIC_REPO_URL,
        "itch_url": ITCH_URL,
        "kofi_url": KOFI_URL,
        "support_email": SUPPORT_EMAIL,
    }
    for base in (get_bundle_dir(), get_user_data_dir()):
        candidate = base / "BUILD_INFO.json"
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8-sig") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    # Normalizaciones retrocompatibles
                    if "git_commit_sha" in loaded and "git_commit" not in loaded:
                        loaded["git_commit"] = loaded["git_commit_sha"]
                    if "built_at_utc" in loaded and "build_date" not in loaded:
                        loaded["build_date"] = loaded["built_at_utc"]
                    if "version" in loaded:
                        loaded["version"] = clean_version(str(loaded["version"]))
                    if "repository_url" not in loaded and "repository" in loaded:
                        repo_val = str(loaded["repository"]).strip()
                        if repo_val.startswith("http"):
                            loaded["repository_url"] = repo_val
                        else:
                            loaded["repository_url"] = f"https://github.com/{repo_val}"
                    default_info.update(loaded)
                    break
            except Exception:
                pass
    return default_info


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe filename fragment from `name`.

    Removes characters forbidden on Windows (<>:\"/\\|?* and control chars),
    collapses whitespace, trims trailing dots/spaces, and limits length.
    """
    if not name:
        return "campaign"

    cleaned = re.sub(r"\s+", " ", name).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', cleaned)
    cleaned = cleaned.replace(os.path.sep, "_")
    cleaned = cleaned.rstrip(" .")
    cleaned = re.sub(r"_+", "_", cleaned)
    max_len = 200
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    if not cleaned:
        return "campaign"
    return cleaned


#: Conectores que no deben quedar colgando al final de un slug recortado.
_CONECTORES_SLUG = frozenset({
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "y", "o", "u", "a", "al", "en", "con", "sin", "por", "para", "que",
    "su", "sus", "mi", "mis", "tu", "tus", "como", "sobre", "entre",
})


def slugify(name: str, max_len: int = 48) -> str:
    """Return a short, ASCII-only slug suitable for a campaign folder name.

    "El Último Compás de los Muertos" -> "el-ultimo-compas-de-los-muertos".
    Cuts at a word boundary (never mid-word) and drops dangling connectors, so
    the folder stays legible and well under Windows' path limits.
    """
    base = unicodedata.normalize("NFKD", str(name or ""))
    base = "".join(c for c in base if not unicodedata.combining(c))
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    if not base:
        return "campana"
    if len(base) > max_len:
        recorte = base[:max_len]
        if "-" in recorte:
            recorte = recorte.rsplit("-", 1)[0]
        partes = [p for p in recorte.strip("-").split("-") if p]
        while len(partes) > 1 and partes[-1] in _CONECTORES_SLUG:
            partes.pop()
        base = "-".join(partes) or base[:max_len].strip("-")
    return base or "campana"


def get_debug_dir(campaign_folder: str | None = None, campaign_slug: str | None = None) -> str:
    """Return a path to store debug files.

    If `campaign_folder` is provided and exists, the debug directory will be
    `campaign_folder/debug`. Otherwise checks if debug_logger has an active campaign,
    or falls back to `saves/debug`.
    """
    base_saves = get_user_data_dir() / "saves"
    if campaign_folder and os.path.isdir(campaign_folder):
        debug_dir = os.path.join(campaign_folder, 'debug')
    else:
        try:
            from src.llm import debug_logger
            camp_activa = debug_logger.obtener_carpeta_campana()
            if camp_activa and os.path.isdir(str(camp_activa)):
                debug_dir = os.path.join(str(camp_activa), 'debug')
            else:
                debug_dir = str(base_saves / 'debug')
        except Exception:
            debug_dir = str(base_saves / 'debug')
    try:
        os.makedirs(debug_dir, exist_ok=True)
    except Exception:
        pass
    return debug_dir
