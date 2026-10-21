import os
import re
import sys
import json
from pathlib import Path
from typing import Any

APP_FOLDER_NAME = "DnD-Infinite-RPG"


def is_frozen() -> bool:
    """Retorna True si la aplicación se está ejecutando como binario compilado (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


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
    (`saves/`, `logs/`, `exports/`).
    - En modo código fuente: raíz del proyecto.
    - En ejecutable compilado (Itch.io): directorio junto al `.exe` si tiene permisos
      de escritura (modo portable), o `%LOCALAPPDATA%/DnD-Infinite-RPG` como respaldo seguro.
    """
    if not is_frozen():
        return Path(__file__).resolve().parent.parent.parent

    # En macOS, un .app bundle nunca debe escribir dentro de Contents/MacOS
    if sys.platform == "darwin":
        mac_data_dir = Path.home() / "Library" / "Application Support" / APP_FOLDER_NAME
        mac_data_dir.mkdir(parents=True, exist_ok=True)
        return mac_data_dir

    exe_dir = Path(sys.executable).resolve().parent
    # Probar si la carpeta junto al ejecutable es escribible (estándar portable en Itch.io para Windows/Linux)
    try:
        test_dir = exe_dir / "saves"
        test_dir.mkdir(parents=True, exist_ok=True)
        probe = test_dir / ".write_probe"
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        probe.unlink(missing_ok=True)
        return exe_dir
    except OSError:
        # Carpeta protegida (ej. Program Files en Windows o /opt /usr en Linux): usar directorio de datos del usuario
        if sys.platform == "win32":
            appdata = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
            fallback_dir = Path(appdata) / APP_FOLDER_NAME
        else:
            xdg_data = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            fallback_dir = Path(xdg_data) / APP_FOLDER_NAME
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir


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


def get_build_info() -> dict[str, Any]:
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
                with open(candidate, encoding="utf-8-sig") as f:
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
