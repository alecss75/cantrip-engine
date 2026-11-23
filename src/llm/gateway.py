"""
gateway.py - Gateway Resiliente de Modelos de Lenguaje (LLM) Multi-Proveedor

Maneja la comunicación transparente con:
1. Groq Cloud (Ultra-rápido, modelos Open Source)
2. OpenRouter / Requesty (Adaptador Universal OpenAI-Compatible con soporte de caché de prefijo)
3. Google Gemini (Google Generative AI SDK directo)

Características:
- Proveedor 'Sticky' y failover transparente en cascada ante cuotas o errores HTTP.
- Extracción de telemetría de tokens (prompt, completion, cached) y estimación de coste en USD.
- Backoff exponencial con jitter para fallos transitorios de red.
- Listas negras temporales de cuota (HTTP 429).
"""

import os
import sys
import time
import json
import logging
import re
import random
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

logger = logging.getLogger("cantrip.gateway")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.core import credentials
from src.llm import limits as token_limits
from src.llm.limits import LimiteProveedor, PromptDemasiadoGrande, estimar_tokens

load_dotenv()

# Claves de acceso (mantenidas como variables para retrocompatibilidad con imports,
# pero resueltas en caliente mediante credentials.obtener_api_key en cada llamada)
GOOGLE_KEY = credentials.obtener_api_key("gemini")
GROQ_KEY = credentials.obtener_api_key("groq")
ROUTER_KEY = credentials.obtener_api_key("router")
DEEPSEEK_KEY = credentials.obtener_api_key("deepseek")

def _normalizar_proveedor(p: str) -> str:
    return credentials.normalizar_id_proveedor(p)


# Configuración del Router Universal
ROUTER_BASE_URL = os.getenv("ROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "nvidia/nemotron-3.5-lightning:free")

# Configuración de DeepSeek Directo
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Proveedores configurados y jerarquía de failover
PRIMARY_PROVIDER = credentials.obtener_proveedor_primario()
FALLBACK_PROVIDERS = [
    _normalizar_proveedor(p) 
    for p in os.getenv("AI_PROVIDER_FALLBACKS", "deepseek,groq,router,gemini").split(",") 
    if p.strip()
]

# Estado en memoria de listas negras de cuota {provider: expire_timestamp}.
# TICKET-04: todo acceso/mutación pasa por _QUOTA_LOCK. El RLock permite que las
# funciones de limpieza y marcado se llamen entre sí sin bloquearse.
_QUOTA_BLACKLIST: Dict[str, float] = {}
_QUOTA_LOCK = threading.RLock()
_QUOTA_ULTIMA_LIMPIEZA = 0.0
_QUOTA_INTERVALO_LIMPIEZA = 60.0  # segundos entre barridos oportunistas

# Diagnóstico global de la última llamada (compatibilidad). El motor usa el
# parámetro de salida `diagnostico` de generar_con_failover para tener un valor
# aislado por llamada; este global se mantiene bajo lock para lecturas seguras.
_ULTIMO_FALLO_LOCK = threading.RLock()

# Clientes inicializados perezosamente y clave con la que fueron instanciados
_GEMINI_CONFIGURED_KEY: Optional[str] = None
_GROQ_CLIENT = None
_GROQ_CLIENT_KEY: Optional[str] = None


def _ttl_cuota_por_defecto() -> float:
    """TTL de enfriamiento configurado, con fallback seguro si el entorno es inválido."""
    try:
        return max(0.0, float(os.getenv("PROVIDER_QUOTA_TTL", "300")))
    except (TypeError, ValueError):
        return 300.0


def _limpiar_cuotas_expiradas(
    ahora: Optional[float] = None,
    forzar: bool = True,
) -> int:
    """
    Elimina de la lista negra las entradas cuyo TTL ya venció.

    Devuelve el número de claves retiradas. Con `forzar=False` el barrido se
    limita a una vez cada `_QUOTA_INTERVALO_LIMPIEZA` segundos para no recorrer
    el diccionario en cada llamada al gateway. La función es segura entre hilos:
    toma `_QUOTA_LOCK` y el RLock permite invocarla desde `esta_en_cuota` o
    `marcar_cuota` sin deadlocks.
    """
    global _QUOTA_ULTIMA_LIMPIEZA

    ahora = time.time() if ahora is None else float(ahora)
    with _QUOTA_LOCK:
        if not forzar and (ahora - _QUOTA_ULTIMA_LIMPIEZA) < _QUOTA_INTERVALO_LIMPIEZA:
            return 0

        expiradas = [
            proveedor
            for proveedor, expiracion in _QUOTA_BLACKLIST.items()
            if expiracion <= ahora
        ]
        for proveedor in expiradas:
            _QUOTA_BLACKLIST.pop(proveedor, None)

        _QUOTA_ULTIMA_LIMPIEZA = ahora
        return len(expiradas)


def limpiar_cuotas_expiradas() -> int:
    """Fuerza un barrido de la lista negra de cuotas (útil para mantenimiento/tests)."""
    return _limpiar_cuotas_expiradas(forzar=True)


def esta_en_cuota(proveedor: str) -> bool:
    """
    Indica si el proveedor sigue bloqueado por cuota.

    El nombre histórico significa "está en la lista negra de cuota"; devuelve
    True mientras la marca de expiración sea futura.
    """
    ahora = time.time()
    with _QUOTA_LOCK:
        # Barrido oportunista y acotado; después se elimina la clave consultada
        # si venció, para no esperar al siguiente intervalo de limpieza.
        _limpiar_cuotas_expiradas(ahora, forzar=False)
        expiracion = _QUOTA_BLACKLIST.get(proveedor, 0.0)
        if expiracion <= ahora:
            if expiracion:
                _QUOTA_BLACKLIST.pop(proveedor, None)
            return False
        return True


def tiempo_restante_cuota(proveedor: str) -> int:
    """Segundos (redondeados hacia abajo) que faltan para que expire el bloqueo."""
    with _QUOTA_LOCK:
        expiracion = _QUOTA_BLACKLIST.get(proveedor, 0.0)
        return max(0, int(expiracion - time.time()))


def marcar_cuota(proveedor: str, ttl: float, ahora: Optional[float] = None) -> float:
    """
    Marca un proveedor como agotado durante `ttl` segundos y devuelve su expiración.

    La escritura se hace bajo `_QUOTA_LOCK`; si dos hilos reciben un 429 a la vez,
    el último TTL observado (idealmente el `Retry-After` más reciente del
    proveedor) es el que queda vigente sin perder escrituras.
    """
    ahora = time.time() if ahora is None else float(ahora)
    try:
        ttl = max(0.0, float(ttl))
    except (TypeError, ValueError):
        ttl = 0.0

    with _QUOTA_LOCK:
        _limpiar_cuotas_expiradas(ahora, forzar=False)
        expiracion = ahora + ttl
        _QUOTA_BLACKLIST[proveedor] = expiracion
        return expiracion


def _valor_cabecera(headers: Any, nombre: str) -> Any:
    """Busca una cabecera HTTP de forma insensible a mayúsculas/minúsculas."""
    if headers is None:
        return None
    try:
        elementos = headers.items()
    except (AttributeError, TypeError):
        return None
    try:
        for clave, valor in elementos:
            if str(clave).strip().lower() == nombre:
                return valor
    except (TypeError, ValueError):
        return None
    return None


def _retry_after_segundos(exc: Exception) -> Optional[float]:
    """
    Extrae `Retry-After` de una excepción HTTP y la normaliza a segundos.

    Soporta el formato numérico ("120") y el formato HTTP-date
    ("Wed, 21 Oct 2015 07:28:00 GMT"). Busca primero en `exc.headers` y luego en
    `exc.response.headers`, que es donde lo exponen requests, urllib y los SDK
    de Groq/OpenAI. Devuelve None si la cabecera no existe o no es válida.
    """
    fuentes = (
        getattr(exc, "retry_after", None),
        _valor_cabecera(getattr(exc, "headers", None), "retry-after"),
        _valor_cabecera(
            getattr(getattr(exc, "response", None), "headers", None),
            "retry-after",
        ),
    )

    for valor in fuentes:
        if valor is None:
            continue
        if isinstance(valor, bytes):
            valor = valor.decode("utf-8", errors="replace")
        texto = str(valor).strip()
        if not texto:
            continue

        try:
            segundos = float(texto)
            if segundos >= 0:
                return segundos
        except ValueError:
            pass

        try:
            fecha = parsedate_to_datetime(texto)
            if fecha is None:
                continue
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
            return max(0.0, (fecha - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            continue

    return None


def _ttl_cuota_para_error(exc: Exception, ttl_por_defecto: float) -> float:
    """TTL efectivo: `Retry-After` del proveedor si existe, si no el configurado."""
    retry_after = _retry_after_segundos(exc)
    return ttl_por_defecto if retry_after is None else retry_after


def reiniciar_clientes_llm() -> None:
    """Reinicia clientes cacheados y listas negras cuando el usuario actualiza sus API Keys en caliente."""
    global _GEMINI_CONFIGURED_KEY, _GROQ_CLIENT, _GROQ_CLIENT_KEY, PRIMARY_PROVIDER
    global GOOGLE_KEY, GROQ_KEY, ROUTER_KEY, DEEPSEEK_KEY, _QUOTA_ULTIMA_LIMPIEZA
    _GEMINI_CONFIGURED_KEY = None
    _GROQ_CLIENT = None
    _GROQ_CLIENT_KEY = None
    with _QUOTA_LOCK:
        _QUOTA_BLACKLIST.clear()
        _QUOTA_ULTIMA_LIMPIEZA = time.time()
    GOOGLE_KEY = credentials.obtener_api_key("gemini")
    GROQ_KEY = credentials.obtener_api_key("groq")
    ROUTER_KEY = credentials.obtener_api_key("router")
    DEEPSEEK_KEY = credentials.obtener_api_key("deepseek")
    PRIMARY_PROVIDER = credentials.obtener_proveedor_primario()


from . import debug_logger

class LLMResponse:
    """Envoltura universal para estandarizar respuestas y telemetría de todos los proveedores."""
    def __init__(
        self,
        text: str,
        raw_response: Any = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        provider_used: str = "",
        model_used: str = "",
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        call_id: int = 0
    ):
        self.text = text
        self.raw_response = raw_response
        self.tool_calls = tool_calls or []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens
        self.provider_used = provider_used
        self.model_used = model_used
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
        self.call_id = call_id
        # Telemetría de presupuesto de tokens (TICKET-07). Los rellena el gateway.
        self.modo_prompt = "completo"
        self.prompt_chars = 0
        self.prompt_tokens_estimados = 0
        self.limite_proveedor: Optional[int] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _es_horario_peak_deepseek(ahora_utc: Optional[datetime] = None) -> bool:
    """Devuelve True si la hora UTC cae en ventana Peak de DeepSeek (lun-vie 01-04 y 06-10 UTC)."""
    dt = ahora_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.weekday() >= 5:  # Sábado o domingo -> Off-Peak
        return False
    h = dt.hour
    return (1 <= h < 4) or (6 <= h < 10)


def _calcular_coste_estimado(
    provider: str,
    model: str,
    prompt_tok: int,
    comp_tok: int,
    cached_tok: int,
    ahora_utc: Optional[datetime] = None,
) -> float:
    """Calcula una estimación de coste en USD según proveedor, modelo y franja UTC."""
    m_lower = (model or "").lower()
    p_lower = (provider or "").lower()

    # Si es modelo gratuito (ej. OpenRouter con tag :free) o Groq en capa gratuita
    if ":free" in m_lower or "groq" in p_lower:
        return 0.0

    # Tarifas estimadas por millón de tokens ($ / 1M tokens)
    # Formato: (input_normal, input_cached, output)
    rates = (0.50, 0.05, 1.50)  # Default conservador

    if "gemini-3.8-flash" in m_lower:
        rates = (0.75, 0.075, 3.75)
    elif "gemini-2.5-flash" in m_lower or "gemini-2.0-flash" in m_lower or "gemini-flash" in m_lower:
        rates = (0.15, 0.0375, 0.60)
    elif "deepseek-v4-pro" in m_lower or "deepseek-pro" in m_lower:
        rates = (1.32, 0.044, 3.96) if _es_horario_peak_deepseek(ahora_utc) else (0.66, 0.022, 1.98)
    elif "deepseek-reasoner" in m_lower or "deepseek-r1" in m_lower:
        rates = (0.55, 0.14, 2.19)
    elif "deepseek" in m_lower or p_lower == "deepseek":
        rates = (0.30, 0.006, 1.20) if _es_horario_peak_deepseek(ahora_utc) else (0.15, 0.003, 0.60)
    elif "glm-4.5" in m_lower:
        rates = (0.60, 0.11, 2.20)
    elif "glm-5" in m_lower:
        rates = (1.40, 0.26, 4.40)
    elif "claude" in m_lower and "opus" in m_lower:
        rates = (4.00, 0.20, 20.00)
    elif "claude" in m_lower:
        rates = (3.00, 0.30, 15.00)

    # Tokens normales de entrada (descontando cacheados)
    normal_input = max(0, prompt_tok - cached_tok)
    cost = (
        (normal_input * rates[0] / 1_000_000.0) +
        (cached_tok * rates[1] / 1_000_000.0) +
        (comp_tok * rates[2] / 1_000_000.0)
    )
    return round(cost, 6)


def _es_error_cuota(exc: Exception) -> bool:
    """Determina si una excepción corresponde estrictamente a agotamiento de cuota, saldo o 429."""
    # Un prompt demasiado grande (413/TPM) NO es agotamiento de cuota: reintentar el mismo
    # texto es inútil y marcar el proveedor en la lista negra sería incorrecto.
    if isinstance(exc, PromptDemasiadoGrande):
        return False

    err_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()

    if "resourceexhausted" in exc_type or "ratelimit" in exc_type:
        return True
    if "429" in err_str:
        return True
    if "402" in err_str or "insufficient balance" in err_str or "balance" in err_str:
        return True
    if "quota exceeded" in err_str or "rate limit" in err_str or "credits" in err_str:
        return True

    return False


class _ErrorHTTPProveedor(RuntimeError):
    """
    Error HTTP con metadatos suficientes para reintentos granulares.

    Conserva el código de estado, las cabeceras (incluida `Retry-After`) y el
    cuerpo original. Las llamadas que usan `requests`/`urllib` lo lanzan para no
    perder esa información al cruzar el límite del adaptador.
    """

    def __init__(
        self,
        mensaje: str,
        *,
        status_code: Optional[int] = None,
        headers: Any = None,
        body: Optional[str] = None,
    ):
        super().__init__(mensaje)
        self.status_code = status_code
        try:
            self.headers = dict(headers) if headers is not None else {}
        except (TypeError, ValueError):
            self.headers = headers
        self.body = body


def _error_http(
    etiqueta: str,
    status_code: int,
    cuerpo: str,
    headers: Any = None,
) -> _ErrorHTTPProveedor:
    """Construye un error HTTP preservando `Retry-After` y el cuerpo del proveedor."""
    return _ErrorHTTPProveedor(
        f"HTTP {status_code} {etiqueta}: {str(cuerpo)[:120]}",
        status_code=status_code,
        headers=headers,
        body=cuerpo,
    )


# Último fallo de una llamada a `generar_con_failover` (diagnóstico para el motor).
# Permite distinguir "el turno no cabe en el proveedor" de "el DM no respondió" sin
# cambiar el contrato de retorno (Optional[LLMResponse]).
# TICKET-04: el motor recibe el fallo por llamada mediante `diagnostico`; este global
# se conserva solo por compatibilidad y se lee/escribe bajo `_ULTIMO_FALLO_LOCK`.
_ULTIMO_FALLO: Optional[Exception] = None


def _fijar_ultimo_fallo(exc: Optional[Exception]) -> None:
    """Actualiza el diagnóstico global de forma atómica (compatibilidad)."""
    global _ULTIMO_FALLO
    with _ULTIMO_FALLO_LOCK:
        _ULTIMO_FALLO = exc


def obtener_ultimo_fallo() -> Optional[Exception]:
    """Devuelve el motivo del último fallo total registrado por `generar_con_failover`."""
    with _ULTIMO_FALLO_LOCK:
        return _ULTIMO_FALLO


def cadena_de_proveedores() -> List[str]:
    """Orden efectivo de proveedores (primario + fallbacks sin duplicados)."""
    orden: List[str] = []
    if PRIMARY_PROVIDER:
        orden.append(_normalizar_proveedor(PRIMARY_PROVIDER))
    for p in FALLBACK_PROVIDERS:
        if p not in orden:
            orden.append(p)
    return orden


def _status_http(exc: Exception) -> Optional[int]:
    """Extrae el código HTTP de una excepción de SDK (Groq/OpenAI/requests/urllib)."""
    for candidato in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "code", None),
    ):
        try:
            if candidato is not None:
                return int(candidato)
        except (TypeError, ValueError):
            continue
    return None


def _cuerpo_error(exc: Exception) -> str:
    """Texto del cuerpo de error (dict, objeto HTTP o string) para clasificar el fallo."""
    for fuente in (getattr(exc, "body", None), getattr(exc, "response", None)):
        if fuente is None:
            continue
        if isinstance(fuente, dict):
            try:
                return json.dumps(fuente, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(fuente)
        if isinstance(fuente, str):
            return fuente
        texto = getattr(fuente, "text", None) or getattr(fuente, "message", None)
        if isinstance(texto, str) and texto.strip():
            return texto
    return ""


def _es_prompt_demasiado_grande(exc: Exception) -> bool:
    """
    Detecta el rechazo por tamaño de petición / TPM (HTTP 413 o cuota de tokens).

    Groq devuelve 413 con un cuerpo del tipo:
        "Request too large for model ... on tokens per minute (TPM): Limit 8000,
         Requested 10861, please reduce your message size..."
    """
    if isinstance(exc, PromptDemasiadoGrande):
        return True

    cuerpo = _cuerpo_error(exc).lower()
    texto = str(exc).lower()
    completo = f"{texto} {cuerpo}"

    if _status_http(exc) == 413:
        return True
    if "request too large" in completo or "prompt is too large" in completo:
        return True
    if "tokens per minute" in completo or "(tpm)" in completo:
        return True
    if "rate_limit_exceeded" in completo and "token" in completo:
        return True
    if "limit" in completo and "requested" in completo and "token" in completo:
        return True
    return False


def _extraer_tokens_de_error(exc: Exception) -> tuple[Optional[int], Optional[int]]:
    """Extrae (requested, limit) del cuerpo de error de Groq si están disponibles."""
    texto = f"{_cuerpo_error(exc)} {exc}".lower()
    pedido = re.search(r"requested\s+(\d+)", texto)
    limite = re.search(r"limit\s+(\d+)", texto)
    return (
        int(pedido.group(1)) if pedido else None,
        int(limite.group(1)) if limite else None,
    )


def _config_con_presupuesto(
    generation_config: Optional[Dict[str, Any]],
    limite: Optional[LimiteProveedor],
    tokens_entrada: int,
    modo_prompt: str,
) -> Optional[Dict[str, Any]]:
    """
    Ajusta `max_output_tokens` al presupuesto del proveedor.

    - En modo compacto se respeta el presupuesto de salida del proveedor (1.000 en Groq
      free): entrada + salida ≤ 80 % del TPM.
    - En modo completo solo se recorta si entrada + salida declarada no cabe en el TPM.
    - Los proveedores sin TPM duro (DeepSeek, Gemini, Router) no se tocan.
    """
    if not limite or not limite.tpm:
        return generation_config

    cfg = dict(generation_config or {})
    pedido = cfg.get("max_output_tokens", cfg.get("max_tokens"))
    if pedido is None:
        return generation_config

    try:
        pedido = int(pedido)
    except (TypeError, ValueError):
        return generation_config

    if modo_prompt == "compacto":
        permitido = limite.max_salida
    elif pedido + tokens_entrada > int(limite.tpm * (1.0 - limite.margen)):
        # Recorta la salida justo hasta el colchón del TPM: entrada + salida ≤ 90 %.
        permitido = int(limite.tpm * (1.0 - limite.margen)) - tokens_entrada
    else:
        return generation_config

    nuevo = max(1, min(pedido, int(permitido)))
    if nuevo == pedido:
        return generation_config

    cfg.pop("max_tokens", None)
    cfg["max_output_tokens"] = nuevo
    return cfg


def _seleccionar_prompt_para_proveedor(
    proveedor: str,
    prompt: str,
    prompt_compacto: Optional[str],
    limite: Optional[LimiteProveedor],
    modo_compacto: str,
) -> tuple[str, str]:
    """
    Decide qué texto enviar a un proveedor según su presupuesto de tokens.

    Reglas (TICKET-07 §4.6):
    0. `GROQ_COMPACT_MODE=on` + proveedor compactable → compacto, aunque el completo quepa.
    1. Sin límite conocido o el completo cabe → prompt completo.
    2. Con límite y el completo no cabe: compacto si cabe; si no, `PromptDemasiadoGrande`
       (el proveedor se salta, nunca se envía algo que ya sabemos que dará 413).
    """
    if limite is None or not limite.tpm:
        return prompt, "completo"

    presupuesto_completo = limite.presupuesto_entrada or 0
    tokens_completo = estimar_tokens(prompt)
    # `GROQ_COMPACT_MODE=off` desactiva también el compacto de emergencia: reproduce el
    # comportamiento anterior para diagnóstico, con un mensaje accionable.
    admite_compacto = bool(limite.compacto and prompt_compacto and modo_compacto != "off")
    objetivo_compacto = limite.presupuesto_compacto or presupuesto_completo
    tokens_compacto = estimar_tokens(prompt_compacto) if admite_compacto else None

    if modo_compacto == "on" and admite_compacto:
        if tokens_compacto is not None and tokens_compacto <= objetivo_compacto:
            return prompt_compacto, "compacto"
        # Degenerado: el compacto no cabe pero el completo sí; mejor no fallar el turno.
        if tokens_completo <= presupuesto_completo:
            return prompt, "completo"

    if tokens_completo <= presupuesto_completo:
        return prompt, "completo"

    if admite_compacto and tokens_compacto is not None:
        if tokens_compacto <= objetivo_compacto:
            return prompt_compacto, "compacto"
        raise PromptDemasiadoGrande(
            estimado=tokens_compacto,
            limite=objetivo_compacto,
            proveedor=proveedor,
            detalle="el prompt compacto tampoco cabe en el presupuesto",
            prompt_chars=len(prompt_compacto or ""),
            modo="compacto",
            limite_tpm=limite.tpm,
        )

    detalle = (
        "no hay prompt compacto disponible para este turno"
        if not limite.compacto
        else "el modo compacto está desactivado (GROQ_COMPACT_MODE=off)"
    )
    raise PromptDemasiadoGrande(
        estimado=tokens_completo,
        limite=presupuesto_completo,
        proveedor=proveedor,
        detalle=detalle,
        prompt_chars=len(prompt),
        modo="completo",
        limite_tpm=limite.tpm,
    )


def _limite_para_log(limite: Optional[LimiteProveedor], modo_prompt: str = "completo") -> Optional[int]:
    """Presupuesto de entrada aplicado al proveedor (para telemetría)."""
    if limite is None:
        return None
    if modo_prompt == "compacto":
        return limite.presupuesto_compacto
    return limite.presupuesto_entrada


def _obtener_cliente_groq():
    """Inicializa el cliente de Groq de forma perezosa y recarga si la clave cambió en caliente."""
    global _GROQ_CLIENT, _GROQ_CLIENT_KEY
    clave_actual = GROQ_KEY
    if not clave_actual:
        raise ValueError("GROQ_API_KEY no está configurada.")
    if _GROQ_CLIENT is None or _GROQ_CLIENT_KEY != clave_actual:
        try:
            from groq import Groq
            _GROQ_CLIENT = Groq(api_key=clave_actual)
            _GROQ_CLIENT_KEY = clave_actual
        except ImportError:
            raise ImportError("Librería 'groq' no instalada. Ejecuta: pip install groq")
    return _GROQ_CLIENT


def _asegurar_gemini():
    """Configura el SDK de Google Generative AI con la clave activa en caliente."""
    global _GEMINI_CONFIGURED_KEY
    clave_actual = GOOGLE_KEY
    if not clave_actual:
        raise ValueError("GOOGLE_API_KEY no está configurada.")
    if _GEMINI_CONFIGURED_KEY != clave_actual:
        import google.generativeai as genai
        genai.configure(api_key=clave_actual)
        _GEMINI_CONFIGURED_KEY = clave_actual



def _llamar_gemini(
    prompt: str,
    system_instruction: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None
) -> LLMResponse:
    """Ejecuta una llamada directa a Gemini con configuración de seguridad y telemetría."""
    _asegurar_gemini()
    import google.generativeai as genai
    from google.generativeai.types import HarmCategory, HarmBlockThreshold

    modelo = model_name or os.getenv("MODEL_NAME", "models/gemini-2.5-flash")

    safety_settings = [
        {"category": HarmCategory.HARM_CATEGORY_HARASSMENT, "threshold": HarmBlockThreshold.BLOCK_ONLY_HIGH},
        {"category": HarmCategory.HARM_CATEGORY_HATE_SPEECH, "threshold": HarmBlockThreshold.BLOCK_ONLY_HIGH},
        {"category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": HarmBlockThreshold.BLOCK_ONLY_HIGH},
        {"category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": HarmBlockThreshold.BLOCK_ONLY_HIGH},
    ]

    cfg = {
        "temperature": 0.8,
        "top_p": 0.95,
        "max_output_tokens": 2048,
    }
    if generation_config:
        cfg.update(generation_config)

    kwargs = {"generation_config": cfg, "safety_settings": safety_settings}
    if system_instruction:
        kwargs["system_instruction"] = system_instruction

    try:
        model_instance = genai.GenerativeModel(modelo, **kwargs)
    except TypeError:
        kwargs.pop("system_instruction", None)
        model_instance = genai.GenerativeModel(modelo, **kwargs)

    resp = model_instance.generate_content(prompt)
    texto = getattr(resp, "text", "")

    # Extracción de telemetría de tokens
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    usage = getattr(resp, "usage_metadata", None)
    if usage:
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0

    cost_usd = _calcular_coste_estimado("gemini", modelo, prompt_tokens, completion_tokens, cached_tokens)

    return LLMResponse(
        text=texto,
        raw_response=resp,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        provider_used="gemini",
        model_used=modelo,
        cost_usd=cost_usd
    )


def _llamar_groq(
    prompt: str,
    system_instruction: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None
) -> LLMResponse:
    """Ejecuta una llamada directa a Groq Cloud con telemetría de uso."""
    client = _obtener_cliente_groq()
    
    candidatos_env = os.getenv("GROQ_MODEL_CANDIDATES", "")
    lista_candidatos = [m.strip() for m in candidatos_env.split(",") if m.strip()] if candidatos_env else []

    modelos_a_probar = [model_name] if model_name else (
        [os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")]
        + lista_candidatos
        + ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
    )
    modelos = []
    for m in modelos_a_probar:
        if m and m not in modelos:
            modelos.append(m)

    temp = 0.7
    max_tokens = 4096
    if generation_config:
        temp = generation_config.get("temperature", temp)
        max_tokens = generation_config.get("max_output_tokens", generation_config.get("max_tokens", max_tokens))

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    else:
        messages.append({
            "role": "system", 
            "content": "Eres un Dungeon Master de D&D 5e experto. Responde estrictamente con el formato solicitado."
        })

    messages.append({"role": "user", "content": prompt})

    ultimo_error = None
    error_cuota = None
    for mod in modelos:
        try:
            completion = client.chat.completions.create(
                model=mod,
                messages=messages,
                temperature=temp,
                max_tokens=max_tokens,
            )
            msg = completion.choices[0].message
            texto = msg.content or getattr(msg, "reasoning", "") or ""
            if texto.strip():
                usage = getattr(completion, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0 if usage else 0

                return LLMResponse(
                    text=texto,
                    raw_response=completion,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=0,
                    provider_used="groq",
                    model_used=mod,
                    cost_usd=0.0
                )
        except Exception as e:
            ultimo_error = e
            if error_cuota is None and _es_error_cuota(e):
                error_cuota = e
            logger.warning("Groq modelo '%s' falló: %s", mod, str(e)[:200])

            # 413 / TPM exceeded: el TPM es de la organización, no del modelo. Probar
            # el siguiente candidato daría el mismo error y gastaría otra petición,
            # así que se aborta de inmediato con un error accionable.
            if _es_prompt_demasiado_grande(e):
                pedido, limite_tpm = _extraer_tokens_de_error(e)
                raise PromptDemasiadoGrande(
                    estimado=pedido or estimar_tokens(prompt),
                    limite=limite_tpm or 0,
                    proveedor="groq",
                    detalle="el proveedor rechazó el prompt por tamaño (413/TPM)",
                    modelo=mod,
                    prompt_chars=len(prompt or ""),
                    limite_tpm=limite_tpm,
                ) from e

            continue

    if error_cuota is not None:
        raise error_cuota
    if ultimo_error:
        raise ultimo_error
    raise RuntimeError("Groq no devolvió respuesta.")


def _llamar_router_universal(
    prompt: str,
    system_instruction: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None
) -> LLMResponse:
    """
    Adaptador Universal OpenAI-Compatible para OpenRouter y Requesty.ai.
    Utiliza requests (con fallback a urllib) para llamadas HTTP estándar.
    Soporta lista de candidatos (ROUTER_MODEL_CANDIDATES) con rotación automática
    ante saturación (HTTP 429/503) o indisponibilidad temporal.
    """
    clave_router = ROUTER_KEY
    if not clave_router:
        raise ValueError("ROUTER_API_KEY (o OPENROUTER_API_KEY / REQUESTY_API_KEY) no está configurada.")


    if not credentials.validar_url_proveedor(ROUTER_BASE_URL):
        raise ValueError(f"Endpoint bloqueado por seguridad BYOK (fuera de lista blanca HTTPS): {ROUTER_BASE_URL}")

    endpoint = f"{ROUTER_BASE_URL}/chat/completions"

    candidatos_env = os.getenv("ROUTER_MODEL_CANDIDATES", "")
    lista_candidatos = [m.strip() for m in candidatos_env.split(",") if m.strip()] if candidatos_env else []

    modelos_a_probar = [model_name] if model_name else (
        [os.getenv("ROUTER_MODEL", ROUTER_MODEL)]
        + lista_candidatos
    )
    modelos = []
    for m in modelos_a_probar:
        if m and m not in modelos:
            modelos.append(m)

    if not modelos:
        modelos = [ROUTER_MODEL]

    temp = 0.8
    max_tokens = 2048
    if generation_config:
        temp = generation_config.get("temperature", temp)
        max_tokens = generation_config.get("max_output_tokens", generation_config.get("max_tokens", max_tokens))

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    else:
        messages.append({
            "role": "system",
            "content": "Eres un Dungeon Master de D&D 5e experto. Responde estrictamente con el formato solicitado."
        })
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {clave_router}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/alecss75/dnd-gemini-rpg",
        "X-Title": "Cantrip Engine"
    }

    ultimo_error = None
    error_cuota = None
    for modelo in modelos:
        payload = {
            "model": modelo,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tokens
        }

        try:
            data = None
            try:
                import requests
                resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
                status_code = resp.status_code
                if status_code == 429:
                    raise _error_http(
                        f"RateLimit/QuotaExceeded en Router ({modelo})",
                        status_code,
                        resp.text,
                        headers=getattr(resp, "headers", None),
                    )
                if status_code >= 400:
                    raise _error_http(
                        f"en Router ({modelo})",
                        status_code,
                        resp.text,
                        headers=getattr(resp, "headers", None),
                    )
                data = resp.json()
            except ImportError:
                import urllib.request
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST"
                )
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        data = json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="replace")
                    etiqueta = (
                        f"RateLimit/QuotaExceeded en Router ({modelo})"
                        if e.code == 429
                        else f"en Router ({modelo})"
                    )
                    raise _error_http(
                        etiqueta,
                        e.code,
                        err_body,
                        headers=getattr(e, "headers", None),
                    ) from e

            choices = data.get("choices", []) if data else []
            if not choices:
                raise RuntimeError(f"Router no devolvió opciones válidas para {modelo}: {data}")

            msg = choices[0].get("message", {})
            texto = msg.get("content", "") or ""
            if not texto.strip():
                if msg.get("reasoning") or msg.get("reasoning_content"):
                    raise RuntimeError(f"Router ({modelo}) agotó tokens en razonamiento interno (CoT) sin generar contenido final.")
                raise RuntimeError(f"Router ({modelo}) devolvió contenido vacío.")

            patrones_cot_leak = [
                r"^The player, in the middle of",
                r"\bLet me parse the action\b",
                r"\bThis is essentially a combined maneuver\b",
                r"\bI think the best mechanical interpretation\b",
                r"\bLet me emit ['\"]?rule_of_cool\b",
                r"\bFinal decision: emit\b",
                r"\bWe need answer JSON only\b"
            ]
            if not ("###JSON_STOP###" in texto or "```json" in texto or (texto.strip().startswith("{") and texto.strip().endswith("}"))):
                t_sample = texto[:400]
                if any(re.search(p, t_sample, re.IGNORECASE) for p in patrones_cot_leak):
                    raise RuntimeError(f"Router ({modelo}) generó monólogo interno en inglés (CoT Leak) en vez del formato de juego.")

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cached_tokens = (
                usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                or usage.get("native_tokens_cached", 0)
                or 0
            )

            cost_usd = _calcular_coste_estimado("router", modelo, prompt_tokens, completion_tokens, cached_tokens)

            return LLMResponse(
                text=texto,
                raw_response=data,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                provider_used="router",
                model_used=modelo,
                cost_usd=cost_usd
            )
        except Exception as e:
            ultimo_error = e
            if error_cuota is None and _es_error_cuota(e):
                error_cuota = e
            logger.warning("Router modelo '%s' falló: %s", modelo, str(e)[:200])
            continue

    if error_cuota is not None:
        raise error_cuota
    if ultimo_error:
        raise ultimo_error
    raise RuntimeError("Router no devolvió respuesta.")


def _llamar_deepseek(
    prompt: str,
    system_instruction: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    model_name: Optional[str] = None
) -> LLMResponse:
    """
    Cliente directo para DeepSeek API (https://api.deepseek.com).
    Totalmente compatible con OpenAI Chat Completions API con soporte nativo
    para caché de prefijo (prompt_cache_hit_tokens) y modelos de razonamiento (deepseek-reasoner).
    """
    clave_deepseek = DEEPSEEK_KEY
    if not clave_deepseek:
        raise ValueError("DEEPSEEK_API_KEY no está configurada.")


    if not credentials.validar_url_proveedor(DEEPSEEK_BASE_URL):
        raise ValueError(f"Endpoint bloqueado por seguridad BYOK (fuera de lista blanca HTTPS): {DEEPSEEK_BASE_URL}")

    endpoint = f"{DEEPSEEK_BASE_URL}/chat/completions"

    candidatos_env = os.getenv("DEEPSEEK_MODEL_CANDIDATES", "")
    lista_candidatos = [m.strip() for m in candidatos_env.split(",") if m.strip()] if candidatos_env else []

    modelos_a_probar = [model_name] if model_name else (
        [os.getenv("DEEPSEEK_MODEL", DEEPSEEK_MODEL)]
        + lista_candidatos
        + ["deepseek-chat", "deepseek-reasoner"]
    )
    modelos = []
    for m in modelos_a_probar:
        if m and m not in modelos:
            modelos.append(m)

    if not modelos:
        modelos = [DEEPSEEK_MODEL]

    temp = 0.7
    max_tokens = 4096
    if generation_config:
        temp = generation_config.get("temperature", temp)
        max_tokens = generation_config.get("max_output_tokens", generation_config.get("max_tokens", max_tokens))

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    else:
        messages.append({
            "role": "system",
            "content": "Eres un Dungeon Master de D&D 5e experto. Responde estrictamente con el formato solicitado."
        })
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {clave_deepseek}",
        "Content-Type": "application/json"
    }

    ultimo_error = None
    error_cuota = None
    for modelo in modelos:
        payload = {
            "model": modelo,
            "messages": list(messages),
            "temperature": temp,
            "max_tokens": max_tokens
        }

        # Control de Thinking Mode (https://api-docs.deepseek.com/guides/thinking_mode)
        # Por defecto deshabilitado para evitar monólogos internos de miles de tokens y reducir latencia,
        # salvo que se active explícitamente en DEEPSEEK_THINKING o generation_config
        is_reasoner = "reasoner" in modelo.lower() or "r1" in modelo.lower()
        if is_reasoner:
            payload.pop("temperature", None)
        else:
            thinking_opt = None
            if generation_config and "thinking" in generation_config:
                thinking_opt = generation_config["thinking"]
            else:
                env_thinking = os.getenv("DEEPSEEK_THINKING", "disabled").strip().lower()
                if env_thinking in ("disabled", "off", "0", "false"):
                    thinking_opt = {"type": "disabled"}
                elif env_thinking in ("enabled", "on", "1", "true"):
                    thinking_opt = {"type": "enabled"}

            if thinking_opt:
                payload["thinking"] = thinking_opt
                if thinking_opt.get("type") == "enabled":
                    payload["reasoning_effort"] = os.getenv("DEEPSEEK_REASONING_EFFORT", "low")

        # JSON Output Mode (https://api-docs.deepseek.com/guides/json_mode)
        desea_json = False
        if generation_config:
            if (
                generation_config.get("json_mode")
                or generation_config.get("response_mime_type") == "application/json"
                or generation_config.get("response_format", {}).get("type") == "json_object"
            ):
                desea_json = True

        if not desea_json:
            p_comb = (prompt + " " + (system_instruction or "")).lower()
            if (
                "devuelve exclusivamente un objeto json" in p_comb
                or "exclusivamente un objeto json válido" in p_comb
                or "responde exclusivamente con json" in p_comb
            ):
                desea_json = True

        if desea_json:
            payload["response_format"] = {"type": "json_object"}
            # La documentación de DeepSeek exige que la palabra 'json' aparezca en el prompt o system
            tiene_json = any("json" in m.get("content", "").lower() for m in payload["messages"])
            if not tiene_json:
                payload["messages"].append({"role": "system", "content": "Formato de salida requerido: JSON."})

        try:
            data = None
            try:
                import requests
                resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
                status_code = resp.status_code
                if status_code in (402, 429):
                    raise _error_http(
                        f"Cuota/Saldo insuficiente en DeepSeek ({modelo})",
                        status_code,
                        resp.text,
                        headers=getattr(resp, "headers", None),
                    )
                if status_code >= 400:
                    raise _error_http(
                        f"en DeepSeek ({modelo})",
                        status_code,
                        resp.text,
                        headers=getattr(resp, "headers", None),
                    )
                data = resp.json()
            except ImportError:
                import urllib.request
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST"
                )
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        data = json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="replace")
                    etiqueta = (
                        f"Cuota/Saldo insuficiente en DeepSeek ({modelo})"
                        if e.code in (402, 429)
                        else f"en DeepSeek ({modelo})"
                    )
                    raise _error_http(
                        etiqueta,
                        e.code,
                        err_body,
                        headers=getattr(e, "headers", None),
                    ) from e

            choices = data.get("choices", []) if data else []
            if not choices:
                raise RuntimeError(f"DeepSeek no devolvió opciones válidas para {modelo}: {data}")

            msg = choices[0].get("message", {})
            texto = msg.get("content", "") or ""

            # Filtro Anti-CoT: Nunca usar reasoning_content como contenido narrativo final si content está vacío
            # Si content está vacío y solo hay reasoning_content, el modelo se truncó pensando sin responder.
            if not texto.strip():
                if msg.get("reasoning_content"):
                    raise RuntimeError(f"DeepSeek ({modelo}) agotó tokens en razonamiento interno (CoT) sin generar contenido final.")
                raise RuntimeError(f"DeepSeek ({modelo}) devolvió contenido vacío.")

            # Detección de fuga de CoT en texto: Monólogo en inglés sin formato de juego
            patrones_cot_leak = [
                r"^The player, in the middle of",
                r"\bLet me parse the action\b",
                r"\bThis is essentially a combined maneuver\b",
                r"\bI think the best mechanical interpretation\b",
                r"\bLet me emit ['\"]?rule_of_cool\b",
                r"\bFinal decision: emit\b",
                r"\bWe need answer JSON only\b"
            ]
            if not ("###JSON_STOP###" in texto or "```json" in texto or (texto.strip().startswith("{") and texto.strip().endswith("}"))):
                t_sample = texto[:400]
                if any(re.search(p, t_sample, re.IGNORECASE) for p in patrones_cot_leak):
                    raise RuntimeError(f"DeepSeek ({modelo}) generó monólogo interno en inglés (CoT Leak) en vez del formato de juego.")

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cached_tokens = (
                usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                or usage.get("prompt_cache_hit_tokens", 0)
                or 0
            )

            cost_usd = _calcular_coste_estimado("deepseek", modelo, prompt_tokens, completion_tokens, cached_tokens)

            return LLMResponse(
                text=texto,
                raw_response=data,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                provider_used="deepseek",
                model_used=modelo,
                cost_usd=cost_usd
            )
        except Exception as e:
            ultimo_error = e
            if error_cuota is None and _es_error_cuota(e):
                error_cuota = e
            logger.warning("DeepSeek modelo '%s' falló: %s", modelo, str(e)[:200])
            continue

    if error_cuota is not None:
        raise error_cuota
    if ultimo_error:
        raise ultimo_error
    raise RuntimeError("DeepSeek no devolvió respuesta.")


def generar_con_failover(
    prompt: str,
    system_instruction: Optional[str] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    max_retries_per_provider: int = 2,
    descripcion_contexto: str = "Turno de Juego",
    prompt_compacto: Optional[str] = None,
    limites: Optional[Dict[str, LimiteProveedor]] = None,
    diagnostico: Optional[Dict[str, Any]] = None,
) -> Optional[LLMResponse]:
    """
    Función maestra de ejecución con failover resiliente y telemetría de depuración.

    Jerarquía activa por defecto:
    1. DeepSeek Directo (Ultra-económico, alta capacidad de rol y prefijo caché).
    2. Groq Cloud (Ultra-rápido, modelos abiertos de desarrollo).
    3. Router (OpenRouter o Requesty.ai con modelos avanzados).
    4. Gemini Directo (Reserva de emergencia ante cuotas o caídas).

    Presupuesto de tokens (TICKET-07):
    - `prompt_compacto`: variante reducida del mismo turno. Solo se usa con proveedores
      cuyo `LimiteProveedor.compacto` sea True (hoy, Groq free).
    - `limites`: registro de presupuestos por proveedor; por defecto
      `src.llm.limits.limites_por_defecto()`.
    Nunca se envía a un proveedor un prompt que se sabe que excederá su TPM: si no cabe
    ni el compacto, el proveedor se salta y se registra `PromptDemasiadoGrande`.

    Diagnóstico thread-safe (TICKET-04):
    - `diagnostico`: diccionario propiedad del llamador que se rellena con
      `ultimo_fallo` al terminar. Permite leer el motivo en llamadas concurrentes
      sin depender del estado global de `obtener_ultimo_fallo()`.
    """
    call_id = debug_logger._obtener_siguiente_call_id()
    ttl_cuota = _ttl_cuota_por_defecto()

    if diagnostico is not None:
        diagnostico.clear()
        diagnostico["ultimo_fallo"] = None

    modo_compacto = token_limits.modo_compacto()
    mapa_limites = limites if limites is not None else token_limits.limites_por_defecto()

    # Ordenar proveedores según configuración activa
    orden_proveedores = cadena_de_proveedores()

    ultimo_error = None
    fallo_prompt_grande: Optional[PromptDemasiadoGrande] = None

    for proveedor in orden_proveedores:
        # Verificar si el proveedor está temporalmente bloqueado por cuota.
        # `esta_en_cuota` toma el lock y limpia de paso las marcas vencidas.
        if esta_en_cuota(proveedor):
            restante = tiempo_restante_cuota(proveedor)
            debug_logger.registrar_error(
                call_id=call_id,
                contexto=descripcion_contexto,
                proveedor=proveedor,
                intento=0,
                error=RuntimeError(f"Proveedor en lista negra por cuota. Espera {restante}s."),
                es_cuota=True,
                tiempo_bloqueo_s=restante
            )
            continue

        limite = mapa_limites.get(proveedor)

        # --- Selección del texto a enviar (completo o compacto) ---
        try:
            prompt_envio, modo_prompt = _seleccionar_prompt_para_proveedor(
                proveedor=proveedor,
                prompt=prompt,
                prompt_compacto=prompt_compacto,
                limite=limite,
                modo_compacto=modo_compacto,
            )
        except PromptDemasiadoGrande as exc_grande:
            # No es cuota: no se reintenta, no se marca en lista negra, se salta el
            # proveedor y se sigue con el siguiente de la cadena.
            ultimo_error = exc_grande
            fallo_prompt_grande = exc_grande
            debug_logger.registrar_error(
                call_id=call_id,
                contexto=descripcion_contexto,
                proveedor=proveedor,
                intento=0,
                error=exc_grande,
                es_cuota=False,
            )
            continue

        tokens_estimados = estimar_tokens(prompt_envio)
        cfg_envio = _config_con_presupuesto(
            generation_config, limite, tokens_estimados, modo_prompt
        )
        limite_log = _limite_para_log(limite, modo_prompt)

        # Inferencia de modelo esperado para telemetría inicial
        if proveedor == "deepseek":
            modelo_esperado = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        elif proveedor == "groq":
            modelo_esperado = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
        elif proveedor in ("router", "openrouter", "requesty"):
            modelo_esperado = ROUTER_MODEL
        elif proveedor == "gemini":
            modelo_esperado = os.getenv("MODEL_NAME", "models/gemini-2.5-flash")
        else:
            modelo_esperado = "desconocido"

        for intento in range(max_retries_per_provider):
            try:
                debug_logger.registrar_solicitud(
                    call_id=call_id,
                    contexto=descripcion_contexto,
                    proveedor=proveedor,
                    modelo=modelo_esperado,
                    intento=intento + 1,
                    max_intentos=max_retries_per_provider,
                    prompt=prompt_envio,
                    generation_config=cfg_envio,
                    system_instruction=system_instruction,
                    prompt_chars=len(prompt_envio),
                    prompt_tokens_estimados=tokens_estimados,
                    modo_prompt=modo_prompt,
                    limite_proveedor=limite_log,
                )

                t_inicio = time.perf_counter()

                if proveedor == "deepseek":
                    resp = _llamar_deepseek(
                        prompt_envio,
                        system_instruction=system_instruction,
                        generation_config=cfg_envio
                    )
                elif proveedor == "groq":
                    resp = _llamar_groq(
                        prompt_envio,
                        system_instruction=system_instruction,
                        generation_config=cfg_envio
                    )
                elif proveedor in ("router", "openrouter", "requesty"):
                    resp = _llamar_router_universal(
                        prompt_envio,
                        system_instruction=system_instruction,
                        generation_config=cfg_envio
                    )
                elif proveedor == "gemini":
                    resp = _llamar_gemini(
                        prompt_envio,
                        system_instruction=system_instruction,
                        generation_config=cfg_envio
                    )
                else:
                    raise ValueError(f"Proveedor no reconocido: {proveedor}")

                latencia_ms = round((time.perf_counter() - t_inicio) * 1000, 2)

                if resp and resp.text.strip():
                    resp.latency_ms = latencia_ms
                    resp.call_id = call_id
                    resp.modo_prompt = modo_prompt
                    resp.prompt_chars = len(prompt_envio)
                    resp.prompt_tokens_estimados = tokens_estimados
                    resp.limite_proveedor = limite_log
                    debug_logger.registrar_exito(
                        call_id=call_id,
                        contexto=descripcion_contexto,
                        proveedor=proveedor,
                        modelo=resp.model_used,
                        intento=intento + 1,
                        latency_ms=latencia_ms,
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        cached_tokens=resp.cached_tokens,
                        cost_usd=resp.cost_usd,
                        response_text=resp.text,
                        prompt_chars=len(prompt_envio),
                        prompt_tokens_estimados=tokens_estimados,
                        modo_prompt=modo_prompt,
                        limite_proveedor=limite_log,
                    )
                    return resp
                else:
                    raise RuntimeError(f"Respuesta vacía devuelta por {proveedor.upper()}")

            except Exception as e:
                ultimo_error = e
                es_cuota = _es_error_cuota(e)
                err_msg = str(e)
                # TICKET-04: si el proveedor envía Retry-After, ese TTL manda sobre
                # el valor estático de PROVIDER_QUOTA_TTL.
                ttl_aplicado = _ttl_cuota_para_error(e, ttl_cuota) if es_cuota else None

                debug_logger.registrar_error(
                    call_id=call_id,
                    contexto=descripcion_contexto,
                    proveedor=proveedor,
                    intento=intento + 1,
                    error=e,
                    es_cuota=es_cuota,
                    tiempo_bloqueo_s=ttl_aplicado
                )

                # 413/TPM del proveedor: no reintentar el mismo prompt ni bloquearlo
                # por cuota (no hay cuota agotada, hay un texto demasiado grande).
                if isinstance(e, PromptDemasiadoGrande):
                    fallo_prompt_grande = e
                    break

                # Si no hay clave configurada para el proveedor, saltar inmediatamente al siguiente sin reintentos
                if "no está configurada" in err_msg.lower():
                    break

                # Si es un límite de cuota (HTTP 429/402), bloquearlo con el TTL
                # dinámico y conmutar de inmediato.
                if es_cuota:
                    marcar_cuota(proveedor, ttl_aplicado or 0.0)
                    break

                # Si es un error transitorio de red, aplicar backoff exponencial
                if intento < max_retries_per_provider - 1:
                    delay = min((2 ** intento) + random.uniform(0.1, 0.5), 5.0)
                    time.sleep(delay)

    # El diagnóstico prioriza el fallo por tamaño: es el que explica el problema real
    # al jugador aunque otro proveedor haya fallado por estar sin configurar.
    fallo_total = fallo_prompt_grande or ultimo_error
    _fijar_ultimo_fallo(fallo_total)
    if diagnostico is not None:
        diagnostico["ultimo_fallo"] = fallo_total
    debug_logger.registrar_fallo_total(call_id, descripcion_contexto, fallo_total)
    return None


def probar_conexion_proveedor(proveedor: str) -> tuple[bool, str]:
    """
    Realiza una micro-llamada de verificación (ping de 5 tokens) contra el proveedor
    indicado para validar que la API Key ingresada por el usuario funciona correctamente.
    """
    prov = _normalizar_proveedor(proveedor)
    reiniciar_clientes_llm()
    t0 = time.perf_counter()
    prompt_ping = "Responde únicamente con la palabra OK."
    cfg_ping = {"temperature": 0.1, "max_output_tokens": 16, "max_tokens": 16}
    try:
        if prov == "deepseek":
            resp = _llamar_deepseek(prompt_ping, generation_config=cfg_ping)
        elif prov == "groq":
            resp = _llamar_groq(prompt_ping, generation_config=cfg_ping)
        elif prov == "router":
            resp = _llamar_router_universal(prompt_ping, generation_config=cfg_ping)
        elif prov == "gemini":
            resp = _llamar_gemini(prompt_ping, generation_config=cfg_ping)
        else:
            return False, f"Proveedor desconocido: {proveedor}"

        lat = round((time.perf_counter() - t0) * 1000)
        return True, f"✅ Conexión exitosa con {prov.upper()} ({resp.model_used}) en {lat} ms."
    except Exception as e:
        err_limpio = credentials.redactar_secretos(str(e))
        return False, f"❌ Error al conectar con {prov.upper()}: {err_limpio[:180]}"


def generar_con_reintento(prompt: str, descripcion_contexto: str = "Generación", **kwargs) -> Optional[LLMResponse]:
    """Envoltorio de compatibilidad con config.py."""
    return generar_con_failover(prompt, descripcion_contexto=descripcion_contexto, **kwargs)
