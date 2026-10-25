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
from typing import Any
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

# Estado en memoria de listas negras de cuota {provider: expire_timestamp}
_QUOTA_BLACKLIST: dict[str, float] = {}

# Clientes inicializados perezosamente y clave con la que fueron instanciados
_GEMINI_CONFIGURED_KEY: str | None = None
_GROQ_CLIENT = None
_GROQ_CLIENT_KEY: str | None = None


def reiniciar_clientes_llm() -> None:
    """Reinicia clientes cacheados y listas negras cuando el usuario actualiza sus API Keys en caliente."""
    global _GEMINI_CONFIGURED_KEY, _GROQ_CLIENT, _GROQ_CLIENT_KEY, PRIMARY_PROVIDER
    global GOOGLE_KEY, GROQ_KEY, ROUTER_KEY, DEEPSEEK_KEY
    _GEMINI_CONFIGURED_KEY = None
    _GROQ_CLIENT = None
    _GROQ_CLIENT_KEY = None
    _QUOTA_BLACKLIST.clear()
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
        tool_calls: list[dict[str, Any]] | None = None,
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

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _calcular_coste_estimado(provider: str, model: str, prompt_tok: int, comp_tok: int, cached_tok: int) -> float:
    """Calcula una estimación de coste en USD según proveedor y modelo."""
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
    elif "deepseek-reasoner" in m_lower or "deepseek-r1" in m_lower:
        rates = (0.55, 0.14, 2.19)
    elif "deepseek" in m_lower or p_lower == "deepseek":
        rates = (0.14, 0.014, 0.28)
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
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
    model_name: str | None = None
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
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
    model_name: str | None = None
) -> LLMResponse:
    """Ejecuta una llamada directa a Groq Cloud con telemetría de uso."""
    client = _obtener_cliente_groq()

    candidatos_env = os.getenv("GROQ_MODEL_CANDIDATES", "")
    lista_candidatos = [m.strip() for m in candidatos_env.split(",") if m.strip()] if candidatos_env else []

    modelos_a_probar = [model_name] if model_name else (
        [os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")]
        + lista_candidatos
        + ["llama-3.3-70b-versatile", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
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
            logger.warning("Groq modelo '%s' falló: %s", mod, str(e)[:200])
            continue

    if ultimo_error:
        raise ultimo_error
    raise RuntimeError("Groq no devolvió respuesta.")


def _llamar_router_universal(
    prompt: str,
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
    model_name: str | None = None
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
        "X-Title": "DnD 5e AI Wizard Engine"
    }

    ultimo_error = None
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
                    raise RuntimeError(f"HTTP 429 RateLimit/QuotaExceeded en Router ({modelo}): {resp.text[:120]}")
                if status_code >= 400:
                    raise RuntimeError(f"HTTP {status_code} en Router ({modelo}): {resp.text[:120]}")
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
                    if e.code == 429:
                        raise RuntimeError(f"HTTP 429 RateLimit/QuotaExceeded en Router ({modelo}): {err_body[:120]}")
                    raise RuntimeError(f"HTTP {e.code} en Router ({modelo}): {err_body[:120]}")

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
            logger.warning("Router modelo '%s' falló: %s", modelo, str(e)[:200])
            continue

    if ultimo_error:
        raise ultimo_error
    raise RuntimeError("Router no devolvió respuesta.")


def _llamar_deepseek(
    prompt: str,
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
    model_name: str | None = None
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
                    raise RuntimeError(f"HTTP {status_code} Cuota/Saldo insuficiente en DeepSeek ({modelo}): {resp.text[:120]}")
                if status_code >= 400:
                    raise RuntimeError(f"HTTP {status_code} en DeepSeek ({modelo}): {resp.text[:120]}")
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
                    if e.code in (402, 429):
                        raise RuntimeError(f"HTTP {e.code} Cuota/Saldo insuficiente en DeepSeek ({modelo}): {err_body[:120]}")
                    raise RuntimeError(f"HTTP {e.code} en DeepSeek ({modelo}): {err_body[:120]}")

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
            logger.warning("DeepSeek modelo '%s' falló: %s", modelo, str(e)[:200])
            continue

    if ultimo_error:
        raise ultimo_error
    raise RuntimeError("DeepSeek no devolvió respuesta.")


def generar_con_failover(
    prompt: str,
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
    max_retries_per_provider: int = 2,
    descripcion_contexto: str = "Turno de Juego"
) -> LLMResponse | None:
    """
    Función maestra de ejecución con failover resiliente y telemetría de depuración.
    
    Jerarquía activa por defecto:
    1. DeepSeek Directo (Ultra-económico, alta capacidad de rol y prefijo caché).
    2. Groq Cloud (Ultra-rápido, modelos abiertos de desarrollo).
    3. Router (OpenRouter o Requesty.ai con modelos avanzados).
    4. Gemini Directo (Reserva de emergencia ante cuotas o caídas).
    """
    call_id = debug_logger._obtener_siguiente_call_id()
    now = time.time()
    ttl_cuota = int(os.getenv("PROVIDER_QUOTA_TTL", "300"))

    # Ordenar proveedores según configuración activa
    orden_proveedores = []
    if PRIMARY_PROVIDER:
        orden_proveedores.append(PRIMARY_PROVIDER)
    for p in FALLBACK_PROVIDERS:
        if p not in orden_proveedores:
            orden_proveedores.append(p)


    ultimo_error = None

    for proveedor in orden_proveedores:
        # Verificar si el proveedor está temporalmente bloqueado por cuota
        expiracion_bloqueo = _QUOTA_BLACKLIST.get(proveedor, 0)
        if expiracion_bloqueo > now:
            restante = int(expiracion_bloqueo - now)
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
                    prompt=prompt,
                    generation_config=generation_config,
                    system_instruction=system_instruction
                )

                t_inicio = time.perf_counter()

                if proveedor == "deepseek":
                    resp = _llamar_deepseek(
                        prompt,
                        system_instruction=system_instruction,
                        generation_config=generation_config
                    )
                elif proveedor == "groq":
                    resp = _llamar_groq(
                        prompt,
                        system_instruction=system_instruction,
                        generation_config=generation_config
                    )
                elif proveedor in ("router", "openrouter", "requesty"):
                    resp = _llamar_router_universal(
                        prompt,
                        system_instruction=system_instruction,
                        generation_config=generation_config
                    )
                elif proveedor == "gemini":
                    resp = _llamar_gemini(
                        prompt,
                        system_instruction=system_instruction,
                        generation_config=generation_config
                    )
                else:
                    raise ValueError(f"Proveedor no reconocido: {proveedor}")

                latencia_ms = round((time.perf_counter() - t_inicio) * 1000, 2)

                if resp and resp.text.strip():
                    resp.latency_ms = latencia_ms
                    resp.call_id = call_id
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
                        response_text=resp.text
                    )
                    return resp
                else:
                    raise RuntimeError(f"Respuesta vacía devuelta por {proveedor.upper()}")

            except Exception as e:
                ultimo_error = e
                es_cuota = _es_error_cuota(e)
                err_msg = str(e)

                debug_logger.registrar_error(
                    call_id=call_id,
                    contexto=descripcion_contexto,
                    proveedor=proveedor,
                    intento=intento + 1,
                    error=e,
                    es_cuota=es_cuota,
                    tiempo_bloqueo_s=ttl_cuota if es_cuota else None
                )

                # Si no hay clave configurada para el proveedor, saltar inmediatamente al siguiente sin reintentos
                if "no está configurada" in err_msg.lower():
                    break

                # Si es un límite de cuota (HTTP 429), bloquearlo y conmutar de inmediato
                if es_cuota:
                    _QUOTA_BLACKLIST[proveedor] = time.time() + ttl_cuota
                    break

                # Si es un error transitorio de red, aplicar backoff exponencial
                if intento < max_retries_per_provider - 1:
                    delay = min((2 ** intento) + random.uniform(0.1, 0.5), 5.0)
                    time.sleep(delay)

    debug_logger.registrar_fallo_total(call_id, descripcion_contexto, ultimo_error)
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


def generar_con_reintento(prompt: str, descripcion_contexto: str = "Generación", **kwargs) -> LLMResponse | None:
    """Envoltorio de compatibilidad con config.py."""
    return generar_con_failover(prompt, descripcion_contexto=descripcion_contexto, **kwargs)
