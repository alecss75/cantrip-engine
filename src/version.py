"""
src/version.py — Fuente Canónica Única de Versión y Metadatos de Distribución
"""

__version__ = "1.1.0"
APP_NAME = "DnD-Infinite-RPG"
PUBLIC_REPO = "alecss75/dnd-infinite-rpg"
PUBLIC_REPO_URL = f"https://github.com/{PUBLIC_REPO}"
ITCH_URL = "https://alecss75.itch.io/dnd-infinite-rpg"
KOFI_URL = "https://ko-fi.com/alecss75"
SUPPORT_EMAIL = "alejandrosansoresarjona@gmail.com"


def clean_version(raw: str | None) -> str:
    """Devuelve la versión canónica sin prefijo 'v' (ej. 'v1.0.0' -> '1.0.0')."""
    if not raw:
        return __version__
    cleaned = str(raw).strip().lstrip("vV").strip()
    if not cleaned or cleaned.lower() in ("main", "master", "head", "local"):
        return __version__
    return cleaned


def format_version(raw: str | None = None) -> str:
    """Devuelve la versión con un único prefijo 'v' para mostrar en UI ('v1.0.0')."""
    return f"v{clean_version(raw)}"
