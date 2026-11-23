"""
output_language.py - Idioma de salida del LLM (TICKET-FREE-05A)

Centraliza la normalización del `locale` y la directiva que obliga al modelo a
escribir en inglés la prosa visible para el jugador, sin traducir el contrato
JSON ni los tipos de intención canónicos. Con `locale="es"` la directiva es una
cadena vacía, de modo que los prompts actuales quedan byte-idénticos.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
import locale as _stdlib_locale
import os
import sys
from typing import Any, Iterator

LOCALES_SOPORTADOS = ("es", "en")
LOCALE_POR_DEFECTO = "es"

_ALIAS_INGLES = {"en", "eng", "english", "ingles", "inglés", "en-us", "en-gb", "en_us", "en_gb"}
_WIN_LANG_ENGLISH = 0x09

_DIRECTIVA_EN = (
    "OUTPUT LANGUAGE (MANDATORY):\n"
    "- All player-facing prose (narration, dialogue, descriptions, new NPC/place/item\n"
    "  names, motives, notes, summaries) MUST be written in English.\n"
    "- The JSON block, its keys and the canonical intent types ('atacar', 'mover',\n"
    "  'loot', 'habilidad', ...) MUST remain exactly in Spanish. Do not translate or\n"
    "  rename them.\n"
    "- Do not retranslate previous content; continue the fiction in English from this\n"
    "  turn onward."
)

_DIRECTIVA_EN_SIN_JSON = (
    "OUTPUT LANGUAGE (MANDATORY):\n"
    "- All player-facing prose and text values (campaign title, narration, dialogue,\n"
    "  descriptions, location/NPC/companion names, racial traits, backgrounds, custom\n"
    "  item names, motives, notes, summaries) MUST be written in English.\n"
    "- Keep every JSON key and every canonical identifier (classes, skills, categories,\n"
    "  states) exactly as requested below; only the human-readable values are in English.\n"
    "- Never copy non-English example text verbatim; translate concepts into English."
)


def normalizar_locale(valor: object) -> str:
    """Devuelve 'en' solo para valores equivalentes a inglés; el resto cae a 'es'."""
    if isinstance(valor, str) and valor.strip().lower() in _ALIAS_INGLES:
        return "en"
    return LOCALE_POR_DEFECTO


def _es_etiqueta_ingles(valor: object) -> bool:
    if not isinstance(valor, str):
        return False
    limpio = valor.strip().lower().replace("-", "_")
    if not limpio:
        return False
    base = limpio.split(".", 1)[0].split("@", 1)[0]
    primario = base.split("_", 1)[0]
    return primario in {"en", "eng", "english", "ingles", "inglés"}


def _es_locale_neutro(valor: str) -> bool:
    limpio = valor.strip().lower().split(".", 1)[0].split("@", 1)[0]
    return limpio in {"", "c", "posix"}


def detectar_locale_sistema() -> str:
    """
    Detecta el idioma inicial del sistema operativo ('en' si es inglés, 'es' en otro caso).

    Orden de consulta:
    1. Variables de entorno POSIX (`LC_ALL`, `LC_MESSAGES`, `LANG`) si definen un locale real.
    2. Idioma de interfaz de usuario en Windows (`GetUserDefaultUILanguage`).
    3. `locale.getlocale()` de la biblioteca estándar.
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "").strip()
        if not val or _es_locale_neutro(val):
            continue
        return "en" if _es_etiqueta_ingles(val) else LOCALE_POR_DEFECTO

    if sys.platform == "win32":
        try:
            lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            lang_primario = lang_id & 0x3FF
            if lang_primario == _WIN_LANG_ENGLISH:
                return "en"
            if lang_primario != 0:
                return LOCALE_POR_DEFECTO
        except Exception:
            pass

    try:
        loc_actual, _ = _stdlib_locale.getlocale()
        if _es_etiqueta_ingles(loc_actual):
            return "en"
    except Exception:
        pass

    return LOCALE_POR_DEFECTO


def directiva_idioma(locale: Any, *, json_canonico: bool = True) -> str:
    """Bloque de salida para insertar al inicio del prompt. En 'es' devuelve ''."""
    if normalizar_locale(locale) != "en":
        return ""
    return _DIRECTIVA_EN if json_canonico else _DIRECTIVA_EN_SIN_JSON


def anexar_directiva(prompt: str, locale: Any, *, json_canonico: bool = True) -> str:
    """Antepone la directiva al prompt; en 'es' devuelve el prompt sin cambios."""
    directiva = directiva_idioma(locale, json_canonico=json_canonico)
    if not directiva:
        return prompt
    return f"{directiva}\n\n{prompt}"


# ---------------------------------------------------------------------------
# Locale activo por contexto (hilo/tarea)
# ---------------------------------------------------------------------------
# Los workers de la GUI fijan el locale una vez al empezar `run()` y los
# generadores secundarios (wizards, botín, resúmenes, mapa, NPCs...) lo leen sin
# necesidad de propagar un parámetro por cada capa intermedia.

_locale_activo: ContextVar[str] = ContextVar("locale_salida_llm", default=LOCALE_POR_DEFECTO)


def locale_activo() -> str:
    """Locale de salida del LLM vigente en el contexto actual ('es' por defecto)."""
    return _locale_activo.get()


def fijar_locale(locale: Any) -> str:
    """
    Fija el locale del contexto actual sin restaurarlo (uso: inicio de `run()` de un
    worker, cuyo hilo tiene un contexto propio que muere con él).
    """
    normalizado = normalizar_locale(locale)
    _locale_activo.set(normalizado)
    return normalizado


@contextmanager
def usar_locale(locale: Any) -> Iterator[str]:
    """Fija el locale de salida durante el bloque y lo restaura al salir."""
    normalizado = normalizar_locale(locale)
    token = _locale_activo.set(normalizado)
    try:
        yield normalizado
    finally:
        _locale_activo.reset(token)


def anexar_directiva_activa(prompt: str, *, json_canonico: bool = False) -> str:
    """Antepone la directiva del locale activo (prosa visible; JSON sin tocar)."""
    return anexar_directiva(prompt, locale_activo(), json_canonico=json_canonico)
