"""
credentials.py - Arquitectura de Confianza y Seguridad BYOK (Bring Your Own Key)

Gestiona las claves de API de los proveedores de IA bajo tres principios de seguridad:
1. Prioridad de resolución en caliente:
   a) Memoria RAM volátil de la sesión actual (Modo Efímero / Cero Disco).
   b) Llavero nativo cifrado del Sistema Operativo (`keyring`: Windows Credential Manager / DPAPI,
      macOS Keychain, Linux SecretService).
   c) Variables de entorno / archivo `.env` local (solo lectura para desarrolladores).
2. Lista Blanca Estricta de Dominios (Strict Endpoint Allowlist):
   - Garantiza que ninguna cabecera `Authorization` o `api_key` sea transmitida jamás
     hacia servidores intermediarios o dominios no autorizados.
3. Redacción Criptográfica y Anonimización de Logs (`redactar_secretos`):
   - Enmascara automáticamente cualquier clave activa o patrón de token (`sk-...`, `gsk_...`,
     `AIza...`, `Bearer ...`) y anonimiza rutas de usuario del sistema operativo (`C:\\Users\\<nombre>`)
     antes de escribir en logs o generar paquetes de feedback `.zip`.
"""

from __future__ import annotations

import os
import re
import logging
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

SERVICE_NAME = "Cantrip_Engine"

# Metadatos canónicos de proveedores soportados
PROVIDERS_META: dict[str, dict[str, str]] = {
    "deepseek": {
        "nombre": "DeepSeek Directo",
        "env_var": "DEEPSEEK_API_KEY",
        "portal_url": "https://platform.deepseek.com/api_keys",
        "placeholder": "sk-...",
        "descripcion": "Ultra-económico con caché de prefijo al 90% y excelente narrativa en español.",
    },
    "groq": {
        "nombre": "Groq Cloud (Capa Gratuita Rápida)",
        "env_var": "GROQ_API_KEY",
        "portal_url": "https://console.groq.com/keys",
        "placeholder": "gsk_...",
        "descripcion": "Velocidad instantánea y capa gratuita ideal para empezar a jugar sin coste.",
    },
    "router": {
        "nombre": "OpenRouter / Requesty",
        "env_var": "ROUTER_API_KEY",
        "portal_url": "https://openrouter.ai/keys",
        "placeholder": "sk-or-v1-...",
        "descripcion": "Acceso unificado a cientos de modelos (incluyendo modelos con etiqueta :free).",
    },
    "gemini": {
        "nombre": "Google Gemini AI Studio",
        "env_var": "GOOGLE_API_KEY",
        "portal_url": "https://aistudio.google.com/app/apikey",
        "placeholder": "AIzaSy...",
        "descripcion": "Modelos Gemini 2.5 Flash con gran ventana de contexto y nivel gratuito.",
    },
}

# Lista blanca inmutable de dominios HTTPS autorizados a recibir API Keys
ALLOWED_API_HOSTS: frozenset[str] = frozenset({
    "api.deepseek.com",
    "openrouter.ai",
    "router.requesty.ai",
    "api.groq.com",
    "generativelanguage.googleapis.com",
})

# Almacenamiento volátil en RAM (se destruye al cerrar el proceso)
_SESSION_KEYS_RAM: dict[str, str] = {}
_SESSION_PRIMARY_PROVIDER: str | None = None
_SOLO_MEMORIA_RAM: bool = False

# Expresiones regulares precompiladas para redacción de secretos en logs/excepciones
_REGEX_SECRET_PATTERNS: list[re.Pattern] = [
    # OpenRouter keys (sk-or-v1-...)
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{16,}\b"),
    # DeepSeek / OpenAI style keys (sk-...)
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    # Groq keys (gsk_...)
    re.compile(r"\bgsk_[A-Za-z0-9_-]{16,}\b"),
    # Google AI Studio keys (AIza...)
    re.compile(r"\bAIza[0-9A-Za-z_-]{25,}\b"),
    # Bearer tokens en cabeceras o volcados HTTP
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{14,}", re.IGNORECASE),
    # Parámetros key=... en URLs
    re.compile(r"([?&](?:api_)?key=)[A-Za-z0-9._\-]{14,}", re.IGNORECASE),
]

# Patrón para anonimizar el nombre de usuario del sistema en rutas locales (Windows/macOS/Linux)
_REGEX_USER_PATH_WIN = re.compile(r"[A-Za-z]:\\Users\\[^\\/\s\"']+", re.IGNORECASE)
_REGEX_USER_PATH_POSIX = re.compile(r"/(?:home|Users)/[^/\s\"']+")


def normalizar_id_proveedor(proveedor: str) -> str:
    """Normaliza cualquier alias de proveedor a su identificador canónico."""
    p = (proveedor or "").strip().lower()
    if p in ("openrouter", "requesty", "router"):
        return "router"
    if p in ("deepseek", "deepseek-chat", "deepseek-reasoner", "deepseek-ai"):
        return "deepseek"
    if p in ("google", "gemini"):
        return "gemini"
    if p in ("groq",):
        return "groq"
    return p


def keyring_disponible() -> bool:
    """Comprueba si el llavero nativo del sistema operativo está disponible."""
    try:
        import keyring
        from keyring.backends import fail
        backend = keyring.get_keyring()
        return not isinstance(backend, fail.Keyring)
    except Exception:
        return False


def obtener_modo_solo_ram() -> bool:
    """Indica si el usuario activó el modo de máxima privacidad (solo RAM)."""
    return _SOLO_MEMORIA_RAM


def establecer_modo_solo_ram(activo: bool) -> None:
    """Activa o desactiva el modo volátil de solo memoria RAM."""
    global _SOLO_MEMORIA_RAM
    _SOLO_MEMORIA_RAM = bool(activo)


def _es_clave_placeholder(valor: str | None) -> bool:
    """Detecta valores de plantilla de .env.example que no son claves reales."""
    if not valor:
        return True
    v = valor.strip()
    if len(v) < 8:
        return True
    v_low = v.lower()
    placeholders = (
        "your_",
        "tu_clave",
        "pega_tu_clave",
        "api_key_here",
        "xxx",
        "none",
        "null",
    )
    return any(p in v_low for p in placeholders)


def obtener_api_key(proveedor: str) -> str | None:
    """
    Obtiene la API Key activa de un proveedor siguiendo el orden de confianza:
    1. Memoria RAM de la sesión actual (`_SESSION_KEYS_RAM`).
    2. Llavero nativo del Sistema Operativo (`keyring`), salvo si está en modo solo RAM.
    3. Variables de entorno / `.env` local.
    """
    prov = normalizar_id_proveedor(proveedor)

    # 1. Memoria RAM de sesión
    if prov in _SESSION_KEYS_RAM and not _es_clave_placeholder(_SESSION_KEYS_RAM[prov]):
        return _SESSION_KEYS_RAM[prov].strip()

    # 2. Llavero del Sistema Operativo
    if not _SOLO_MEMORIA_RAM and keyring_disponible():
        try:
            import keyring
            val_kr = keyring.get_password(SERVICE_NAME, f"api_key_{prov}")
            if val_kr and not _es_clave_placeholder(val_kr):
                return val_kr.strip()
        except Exception:
            pass

    # 3. Variables de entorno (.env)
    if prov == "router":
        val_env = (
            os.getenv("ROUTER_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("REQUESTY_API_KEY")
        )
    else:
        meta = PROVIDERS_META.get(prov, {})
        env_var = meta.get("env_var", "")
        val_env = os.getenv(env_var) if env_var else None

    if val_env and not _es_clave_placeholder(val_env):
        return val_env.strip()

    return None


def obtener_origen_clave(proveedor: str) -> str:
    """
    Devuelve una descripción legible de dónde proviene la clave activa del proveedor
    ('Memoria RAM (Volátil)', 'Llavero del Sistema (Cifrado)', 'Archivo .env', o 'No configurada').
    """
    prov = normalizar_id_proveedor(proveedor)
    if prov in _SESSION_KEYS_RAM and not _es_clave_placeholder(_SESSION_KEYS_RAM[prov]):
        return "Memoria RAM (Volátil)"

    if not _SOLO_MEMORIA_RAM and keyring_disponible():
        try:
            import keyring
            val_kr = keyring.get_password(SERVICE_NAME, f"api_key_{prov}")
            if val_kr and not _es_clave_placeholder(val_kr):
                return "Llavero del Sistema (OS Keyring / DPAPI)"
        except Exception:
            pass

    if obtener_api_key(prov):
        return "Variables de Entorno (.env)"

    return "No configurada"


def guardar_api_key(proveedor: str, clave: str, persistir_en_llavero: bool = True) -> tuple[bool, str]:
    """
    Registra una API Key en memoria RAM y, opcionalmente, en el llavero cifrado del SO.
    Si `clave` está vacía, elimina la clave de dicho proveedor.
    """
    prov = normalizar_id_proveedor(proveedor)
    limpia = (clave or "").strip()

    if not limpia:
        _SESSION_KEYS_RAM.pop(prov, None)
        if keyring_disponible():
            try:
                import keyring
                keyring.delete_password(SERVICE_NAME, f"api_key_{prov}")
            except Exception:
                pass
        return True, f"Clave de {prov} eliminada."

    # Guardar siempre en RAM para disponibilidad inmediata en caliente
    _SESSION_KEYS_RAM[prov] = limpia

    if persistir_en_llavero and not _SOLO_MEMORIA_RAM:
        if keyring_disponible():
            try:
                import keyring
                keyring.set_password(SERVICE_NAME, f"api_key_{prov}", limpia)
                return True, "Guardada en el Llavero Seguro del Sistema Operativo."
            except Exception as e:
                return True, f"Guardada en memoria RAM para esta sesión (Llavero no accesible: {e})."
        else:
            return True, "Guardada en memoria RAM para esta sesión (keyring no disponible)."

    # Si el usuario pidió solo RAM, nos aseguramos de borrar cualquier rastro previo del llavero
    if keyring_disponible():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, f"api_key_{prov}")
        except Exception:
            pass

    return True, "Guardada exclusivamente en Memoria RAM (se borrará al cerrar el juego)."


def eliminar_todas_las_claves() -> int:
    """
    Purga absolutamente todas las claves almacenadas en la memoria RAM y en el llavero del SO.
    Retorna el número de proveedores limpiados.
    """
    limpiados = 0
    for prov in PROVIDERS_META:
        tenia = bool(obtener_api_key(prov))
        _SESSION_KEYS_RAM.pop(prov, None)
        if keyring_disponible():
            try:
                import keyring
                keyring.delete_password(SERVICE_NAME, f"api_key_{prov}")
            except Exception:
                pass
        if tenia:
            limpiados += 1
    return limpiados


def obtener_proveedor_primario() -> str:
    """Devuelve el proveedor primario configurado por el usuario o el primero con clave disponible."""
    if _SESSION_PRIMARY_PROVIDER:
        return _SESSION_PRIMARY_PROVIDER

    if not _SOLO_MEMORIA_RAM and keyring_disponible():
        try:
            import keyring
            pref = keyring.get_password(SERVICE_NAME, "primary_provider")
            if pref and normalizar_id_proveedor(pref) in PROVIDERS_META:
                return normalizar_id_proveedor(pref)
        except Exception:
            pass

    env_prov = normalizar_id_proveedor(os.getenv("AI_PROVIDER", "deepseek"))
    if obtener_api_key(env_prov):
        return env_prov

    # Si el proveedor por defecto no tiene clave pero otro sí, elegir el primero con clave
    for p in ("deepseek", "groq", "router", "gemini"):
        if obtener_api_key(p):
            return p

    return env_prov or "deepseek"


def establecer_proveedor_primario(proveedor: str, persistir: bool = True) -> None:
    """Actualiza el proveedor primario activo en caliente."""
    global _SESSION_PRIMARY_PROVIDER
    prov = normalizar_id_proveedor(proveedor)
    if prov not in PROVIDERS_META:
        return
    _SESSION_PRIMARY_PROVIDER = prov
    if persistir and not _SOLO_MEMORIA_RAM and keyring_disponible():
        try:
            import keyring
            keyring.set_password(SERVICE_NAME, "primary_provider", prov)
        except Exception:
            pass


def hay_alguna_clave_configurada() -> bool:
    """Retorna True si existe al menos una API Key válida configurada en cualquier proveedor."""
    return any(obtener_api_key(p) is not None for p in PROVIDERS_META)


def resumen_estado_proveedores() -> dict[str, dict[str, str]]:
    """
    Devuelve el estado de todos los proveedores (sin exponer jamás la clave real,
    solo una vista enmascarada de los últimos 4 caracteres y su origen).
    """
    estado = {}
    for prov, meta in PROVIDERS_META.items():
        key = obtener_api_key(prov)
        if key:
            if len(key) > 8:
                mascara = f"{key[:4]}••••••••{key[-4:]}"
            else:
                mascara = "••••••••"
        else:
            mascara = "Sin configurar"
        estado[prov] = {
            "nombre": meta["nombre"],
            "configurada": "si" if key else "no",
            "mascara": mascara,
            "origen": obtener_origen_clave(prov),
            "portal_url": meta["portal_url"],
        }
    return estado


def validar_url_proveedor(url: str) -> bool:
    """
    Verifica que un endpoint de API utilice obligatoriamente HTTPS y pertenezca
    a la lista blanca de dominios oficiales (ALLOWED_API_HOSTS).
    Previene exfiltración de claves ante variables BASE_URL manipuladas.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() != "https":
            return False
        host = (parsed.hostname or "").lower()
        return host in ALLOWED_API_HOSTS
    except Exception:
        return False


def redactar_secretos(texto: str, anonimizar_rutas: bool = True) -> str:
    """
    Sanea cualquier cadena de texto reemplazando:
    1. Cualquier valor exacto de API Key actualmente cargado en memoria/entorno.
    2. Cualquier patrón de clave de API (`sk-...`, `gsk_...`, `AIza...`, `Bearer ...`).
    3. Rutas locales con el nombre de usuario del sistema operativo (`C:\\Users\\<usuario>` -> `<USER_HOME>`).
    """
    if not texto or not isinstance(texto, str):
        return texto

    resultado = texto

    # 1. Enmascarar valores literales de claves activas
    for prov in PROVIDERS_META:
        k = obtener_api_key(prov)
        if k and len(k) >= 8 and k in resultado:
            resultado = resultado.replace(k, "[REDACTED_API_KEY]")

    # 2. Enmascarar por patrones regex de tokens/cabeceras
    for pattern in _REGEX_SECRET_PATTERNS:
        if pattern.groups >= 1:
            resultado = pattern.sub(r"\1[REDACTED_API_KEY]", resultado)
        else:
            resultado = pattern.sub("[REDACTED_API_KEY]", resultado)

    # 3. Anonimizar nombres de usuario del sistema operativo en rutas
    if anonimizar_rutas:
        resultado = _REGEX_USER_PATH_WIN.sub("<USER_HOME>", resultado)
        resultado = _REGEX_USER_PATH_POSIX.sub("<USER_HOME>", resultado)

    return resultado


class SecretRedactionFilter(logging.Filter):
    """
    Filtro estándar de `logging` que intercepta cada registro antes de que se escriba
    en consola o en `logs/cantrip.log`, garantizando cero fugas de API Keys.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redactar_secretos(record.msg, anonimizar_rutas=False)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redactar_secretos(v, anonimizar_rutas=False) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redactar_secretos(a, anonimizar_rutas=False) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            pass
        return True
