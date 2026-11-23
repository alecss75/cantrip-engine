"""
i18n.py - Catálogos de presentación de la interfaz (TICKET-FREE-05B)

Fachada mínima de internacionalización ES/EN para la GUI:

- ``t(clave, **kwargs)`` devuelve el texto de presentación en el locale activo.
- Una clave ausente en el locale activo cae al español y, si tampoco existe allí,
  devuelve la propia clave. Nunca lanza excepción y registra un warning una sola
  vez por clave.
- Los catálogos son JSON planos (``clave.con.puntos`` → texto) y solo contienen
  presentación, jamás lógica ni claves canónicas de estado.

Origen de los catálogos, fusionados en este orden (el último gana):

1. ``src/core/locales/<locale>.json``       (catálogo común)
2. ``src/core/locales/<locale>/*.json``     (fragmentos por módulo, orden alfabético)

Los fragmentos por módulo permiten migrar widgets en paralelo sin conflictos de
fusión; el test de paridad (``tests/test_i18n.py``) valida que ES y EN tengan
exactamente las mismas claves.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Set

from src.core.output_language import LOCALES_SOPORTADOS, normalizar_locale

logger = logging.getLogger("cantrip.core.i18n")

LOCALE_BASE = "es"
DIRECTORIO_LOCALES = Path(__file__).resolve().parent / "locales"

_lock = threading.RLock()
_locale_activo: str = LOCALE_BASE
_catalogos: Dict[str, Dict[str, str]] = {}
_faltantes_registradas: Set[str] = set()


def _leer_json(ruta: Path) -> Dict[str, str]:
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("No se pudo leer el catálogo i18n %s: %s", ruta, exc)
        return {}
    if not isinstance(datos, dict):
        logger.warning("El catálogo i18n %s no es un objeto JSON plano.", ruta)
        return {}
    return {str(k): str(v) for k, v in datos.items()}


def _fragmentos(locale: str) -> List[Path]:
    carpeta = DIRECTORIO_LOCALES / locale
    if not carpeta.is_dir():
        return []
    return sorted(carpeta.glob("*.json"))


def cargar_catalogo(locale: str) -> Dict[str, str]:
    """Carga (una sola vez) y devuelve el catálogo fusionado del locale."""
    locale = normalizar_locale(locale)
    with _lock:
        if locale not in _catalogos:
            catalogo: Dict[str, str] = {}
            comun = DIRECTORIO_LOCALES / f"{locale}.json"
            if comun.is_file():
                catalogo.update(_leer_json(comun))
            for fragmento in _fragmentos(locale):
                catalogo.update(_leer_json(fragmento))
            _catalogos[locale] = catalogo
        return _catalogos[locale]


def recargar_catalogos() -> None:
    """Descarta los catálogos en memoria (tests y recarga en caliente)."""
    with _lock:
        _catalogos.clear()
        _faltantes_registradas.clear()


def fijar_locale(locale: object) -> str:
    """Fija el locale activo de la interfaz; valores inválidos caen a 'es'."""
    global _locale_activo
    normalizado = normalizar_locale(locale)
    with _lock:
        _locale_activo = normalizado
    return normalizado


def obtener_locale() -> str:
    """Locale activo de la interfaz ('es' o 'en')."""
    return _locale_activo


def _registrar_faltante(clave: str, locale: str) -> None:
    marca = f"{locale}:{clave}"
    with _lock:
        if marca in _faltantes_registradas:
            return
        _faltantes_registradas.add(marca)
    logger.warning("Clave i18n ausente (%s): %s", locale, clave)


def claves_faltantes() -> List[str]:
    """Claves ausentes registradas hasta ahora, como 'locale:clave'."""
    with _lock:
        return sorted(_faltantes_registradas)


def existe(clave: str, locale: object = None) -> bool:
    """True si la clave existe en el catálogo del locale (sin fallback)."""
    loc = normalizar_locale(locale) if locale is not None else _locale_activo
    return clave in cargar_catalogo(loc)


def t(clave: str, **kwargs: Any) -> str:
    """Texto de presentación para ``clave`` en el locale activo, con fallback a ES."""
    locale = _locale_activo
    texto = cargar_catalogo(locale).get(clave)
    if texto is None:
        _registrar_faltante(clave, locale)
        if locale != LOCALE_BASE:
            texto = cargar_catalogo(LOCALE_BASE).get(clave)
        if texto is None:
            texto = clave
    if kwargs:
        try:
            return texto.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            logger.warning("Parámetros inválidos para la clave i18n %s", clave)
            return texto
    return texto


__all__ = [
    "LOCALES_SOPORTADOS",
    "cargar_catalogo",
    "claves_faltantes",
    "existe",
    "fijar_locale",
    "obtener_locale",
    "recargar_catalogos",
    "t",
]

