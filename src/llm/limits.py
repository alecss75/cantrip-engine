"""
limits.py - Presupuesto de tokens por proveedor y estimador conservador

Centraliza tres piezas que comparten el constructor de prompts (`src/core/prompt_builder.py`)
y el gateway (`src/llm/gateway.py`):

1. `LimiteProveedor`: registro de límites por proveedor (TPM, presupuesto de salida y
   si admite prompt compacto). Groq en capa gratuita es el único proveedor con un TPM
   duro hoy: 8.000 tokens por minuto que se cobran como entrada + salida reservada.
2. `estimar_tokens`: estimador conservador basado en caracteres. No pretende sustituir
   a un tokenizador real (no se añade dependencia); solo debe **no quedarse corto**.
   La constante está calibrada contra `prompt_tokens` reales de Groq free (ver el
   comentario de `CHARS_POR_TOKEN_DEFECTO`) y se aplica un margen extra del 5 %.
3. `PromptDemasiadoGrande`: error accionable que se lanza cuando ni el prompt compacto
   cabe en el presupuesto. Nunca debe presentarse como "el DM no respondió".

Variables de entorno (ver `.env.example`):

    GROQ_TPM_LIMIT=8000              # Dev Tier: súbelo
    GROQ_COMPACT_MODE=auto           # auto | on | off
    GROQ_COMPACT_MAX_INPUT=5400      # objetivo de entrada en modo compacto
    GROQ_COMPACT_MAX_OUTPUT=1000     # presupuesto de salida del turno
    PROMPT_ESTIMATOR_CHARS_PER_TOKEN=3.2
    PROMPT_ESTIMATOR_MARGIN=0.05
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, Optional

# Constantes del estimador.
# Calibración del 2026-10-27 contra Groq free (qwen/qwen3.8-27b), midiendo `prompt_tokens`
# reales de los textos que envía el motor:
#   prefijo estático completo (21.034 chars) → 5.980 tokens → 3,52 chars/token
#   compendio mínimo        (1.364 chars) →   437 tokens → 3,12 chars/token
#   contrato JSON           (3.407 chars) → 1.191 tokens → 2,86 chars/token
#   prosa en español          (337 chars) →   135 tokens → 2,50 chars/token
#   relleno ASCII puro      (4.300 chars) →   578 tokens → 7,44 chars/token
# Un turno compacto mezcla los cuatro primeros, así que 3,2 con el margen del 5 % se
# queda ~10 % por encima de la realidad en la mezcla del motor (nunca por debajo).
# El aporte del 2026-10-27 citaba 4,34 chars/token (38.260 / 8.813), pero esa medida no
# se reproduce con el tokenizador actual: con 4,3 el prompt compacto real ocupaba 6.383
# tokens en vez de los ~5.200 estimados y el turno dejaba de caber en el 80 % del TPM.
CHARS_POR_TOKEN_DEFECTO = 3.5
MARGEN_ESTIMADOR_DEFECTO = 0.05

# Modos de compactación admitidos
MODOS_COMPACTO = ("auto", "on", "off")
MODO_COMPACTO_DEFECTO = "auto"

# Presupuestos por defecto para la capa gratuita de Groq (8.000 TPM)
GROQ_TPM_DEFECTO = 8000
GROQ_MAX_ENTRADA_COMPACTO_DEFECTO = 5400
GROQ_MAX_SALIDA_DEFECTO = 1000

# Salida por defecto de los proveedores sin TPM duro
MAX_SALIDA_AMPLIA = 2048

# Alias de proveedor aceptados en configuración
_ALIAS_PROVEEDOR = {
    "openrouter": "router",
    "requesty": "router",
    "router": "router",
    "google": "gemini",
    "gemini": "gemini",
    "groq": "groq",
    "deepseek": "deepseek",
}


def _env_int(nombre: str, defecto: int) -> int:
    """Lee un entero positivo del entorno, ignorando valores inválidos."""
    try:
        valor = int(str(os.getenv(nombre, "")).strip())
        return valor if valor > 0 else defecto
    except (TypeError, ValueError):
        return defecto


def _env_float(nombre: str, defecto: float, permitir_cero: bool = False) -> float:
    """Lee un float del entorno, ignorando valores inválidos (o no positivos)."""
    try:
        valor = float(str(os.getenv(nombre, "")).strip())
    except (TypeError, ValueError):
        return defecto
    if valor > 0 or (permitir_cero and valor == 0):
        return valor
    return defecto


def normalizar_proveedor(proveedor: str) -> str:
    """Normaliza alias de proveedor sin depender de `src/core/credentials.py`."""
    p = (proveedor or "").strip().lower()
    return _ALIAS_PROVEEDOR.get(p, p)


def chars_por_token() -> float:
    """Caracteres por token calibrados (`PROMPT_ESTIMATOR_CHARS_PER_TOKEN`)."""
    return _env_float("PROMPT_ESTIMATOR_CHARS_PER_TOKEN", CHARS_POR_TOKEN_DEFECTO)


def margen_estimador() -> float:
    """Margen de seguridad del estimador (`PROMPT_ESTIMATOR_MARGIN`, admite 0)."""
    return _env_float("PROMPT_ESTIMATOR_MARGIN", MARGEN_ESTIMADOR_DEFECTO, permitir_cero=True)


def estimar_tokens(texto: str) -> int:
    """
    Estima los tokens de un prompt de forma conservadora (nunca por debajo de la realidad).

    tokens = ceil(len(texto) / chars_por_token * (1 + margen))
    """
    if not texto:
        return 0
    return int(math.ceil((len(texto) / chars_por_token()) * (1.0 + margen_estimador())))


def modo_compacto() -> str:
    """Modo de compactación configurado: `auto`, `on` u `off`."""
    valor = str(os.getenv("GROQ_COMPACT_MODE", MODO_COMPACTO_DEFECTO)).strip().lower()
    return valor if valor in MODOS_COMPACTO else MODO_COMPACTO_DEFECTO


def compacto_activo() -> bool:
    """¿Está permitido construir prompts compactos con la configuración actual?"""
    return modo_compacto() != "off"


class PromptDemasiadoGrande(RuntimeError):
    """
    El prompt estimado no cabe en el presupuesto de tokens por minuto del proveedor.

    No es un error de cuota (429) ni de red: reintentar el mismo prompt no sirve, y
    marcar al proveedor en la lista negra sería incorrecto. El gateway salta el
    proveedor y el motor muestra al jugador un mensaje accionable.
    """

    def __init__(
        self,
        estimado: int,
        limite: int,
        proveedor: str = "",
        detalle: str = "",
        modelo: str = "",
        prompt_chars: Optional[int] = None,
        modo: str = "",
        limite_tpm: Optional[int] = None,
    ):
        self.estimado = int(estimado or 0)
        self.limite = int(limite or 0)
        self.proveedor = (proveedor or "").strip().lower()
        self.detalle = detalle or ""
        self.modelo = modelo or ""
        self.prompt_chars = prompt_chars
        self.modo = modo or ""
        self.limite_tpm = int(limite_tpm) if limite_tpm else self.limite

        partes = []
        if self.proveedor:
            partes.append(f"[{self.proveedor.upper()}]")
        partes.append(
            f"Prompt de ~{self.estimado} tokens > presupuesto de {self.limite} tokens"
        )
        if self.modelo:
            partes.append(f"(modelo {self.modelo})")
        if self.detalle:
            partes.append(f"- {self.detalle}")
        super().__init__(" ".join(partes))

    def a_dict(self) -> Dict[str, object]:
        """Representación serializable para telemetría (`llm_history.jsonl`)."""
        return {
            "tipo": "PromptDemasiadoGrande",
            "proveedor": self.proveedor,
            "modelo": self.modelo,
            "tokens_estimados": self.estimado,
            "limite": self.limite,
            "limite_tpm": self.limite_tpm,
            "prompt_chars": self.prompt_chars,
            "modo_prompt": self.modo,
            "detalle": self.detalle,
        }


@dataclass(frozen=True)
class LimiteProveedor:
    """
    Presupuesto de tokens de un proveedor.

    - `tpm`: tokens por minuto (None = sin límite conocido).
    - `max_salida`: presupuesto de salida reservado en el turno.
    - `compacto`: ¿admite el prompt compacto?
    - `margen`: colchón de seguridad aplicado al TPM (0.10 = 10 %).
    - `max_entrada_compacto`: objetivo de entrada del prompt compacto (None = el
      presupuesto de entrada calculado).
    """

    tpm: Optional[int]
    max_salida: int
    compacto: bool
    margen: float = 0.10
    max_entrada_compacto: Optional[int] = None

    @property
    def presupuesto_entrada(self) -> Optional[int]:
        """Tokens de entrada disponibles en un minuto, descontando salida y margen."""
        if not self.tpm:
            return None
        disponible = int(self.tpm * (1.0 - self.margen)) - int(self.max_salida)
        return max(0, disponible)

    @property
    def presupuesto_compacto(self) -> Optional[int]:
        """Tokens de entrada objetivo cuando se usa el prompt compacto."""
        base = self.presupuesto_entrada
        if self.max_entrada_compacto is None:
            return base
        if base is None:
            return int(self.max_entrada_compacto)
        return min(base, int(self.max_entrada_compacto))

    @property
    def total_maximo(self) -> Optional[int]:
        """Entrada + salida: lo que el proveedor contabiliza contra su TPM."""
        entrada = self.presupuesto_compacto
        if entrada is None:
            return None
        return entrada + int(self.max_salida)


def limites_por_defecto() -> Dict[str, LimiteProveedor]:
    """
    Registro canónico de límites por proveedor.

    Se lee del entorno en cada llamada (barato) para que los tests y la GUI puedan
    ajustar `GROQ_TPM_LIMIT` sin reiniciar el proceso.
    """
    return {
        "groq": LimiteProveedor(
            tpm=_env_int("GROQ_TPM_LIMIT", GROQ_TPM_DEFECTO),
            max_salida=_env_int("GROQ_COMPACT_MAX_OUTPUT", GROQ_MAX_SALIDA_DEFECTO),
            compacto=True,
            max_entrada_compacto=_env_int(
                "GROQ_COMPACT_MAX_INPUT", GROQ_MAX_ENTRADA_COMPACTO_DEFECTO
            ),
        ),
        "deepseek": LimiteProveedor(tpm=None, max_salida=MAX_SALIDA_AMPLIA, compacto=False),
        "gemini": LimiteProveedor(tpm=None, max_salida=MAX_SALIDA_AMPLIA, compacto=False),
        "router": LimiteProveedor(tpm=None, max_salida=MAX_SALIDA_AMPLIA, compacto=False),
    }


def obtener_limite(
    proveedor: str,
    limites: Optional[Dict[str, LimiteProveedor]] = None,
) -> Optional[LimiteProveedor]:
    """Devuelve el límite de un proveedor (o None si no está registrado)."""
    mapa = limites if limites is not None else limites_por_defecto()
    return mapa.get(normalizar_proveedor(proveedor))


def proveedores_compactables(
    limites: Optional[Dict[str, LimiteProveedor]] = None,
) -> Dict[str, LimiteProveedor]:
    """Proveedores con TPM duro que admiten prompt compacto."""
    mapa = limites if limites is not None else limites_por_defecto()
    return {
        prov: limite
        for prov, limite in mapa.items()
        if limite.compacto and limite.tpm
    }


def presupuesto_compacto_global(
    limites: Optional[Dict[str, LimiteProveedor]] = None,
) -> Optional[int]:
    """
    Presupuesto de entrada más estricto entre los proveedores compactables.

    El constructor de prompts usa este número para decidir cuánto recortar antes de
    saber qué proveedor responderá: debe caber en el más restrictivo (Groq free).
    """
    presupuestos = [
        limite.presupuesto_compacto
        for limite in proveedores_compactables(limites).values()
        if limite.presupuesto_compacto is not None
    ]
    return min(presupuestos) if presupuestos else None


def _formato_miles(numero: int) -> str:
    """1234 -> '1.234' (formato español para mensajes al jugador)."""
    return f"{int(numero):,}".replace(",", ".")


def mensaje_prompt_demasiado_grande(
    error: Optional[PromptDemasiadoGrande] = None,
    *,
    tokens_completo: Optional[int] = None,
    tokens_compacto: Optional[int] = None,
    intento_compacto: bool = False,
    proveedor: str = "groq",
    tpm: Optional[int] = None,
) -> str:
    """
    Construye el mensaje accionable que ve el jugador cuando el turno no cabe.

    Nunca decir "el DM no respondió": el problema es de presupuesto del proveedor y
    la salida es configurar Gemini/DeepSeek o subir de tier en Groq.
    """
    prov = normalizar_proveedor(
        (error.proveedor if error and error.proveedor else proveedor) or "groq"
    )
    nombre_prov = {
        "groq": "Groq gratis",
        "deepseek": "DeepSeek",
        "gemini": "Gemini",
        "router": "Router",
    }.get(prov, prov.upper())

    limite_tpm = tpm or (error.limite_tpm if error else None)
    if limite_tpm is None:
        limite_tpm = obtener_limite(prov).tpm if obtener_limite(prov) else None
    limite_tpm = int(limite_tpm or GROQ_TPM_DEFECTO)

    tokens = tokens_completo
    if tokens is None and error is not None:
        tokens = error.estimado

    lineas = []
    if tokens is not None:
        lineas.append(
            f"⚠️ El turno no cabe en {nombre_prov}: ~{_formato_miles(tokens)} tokens "
            f"(límite {_formato_miles(limite_tpm)} TPM)."
        )
    else:
        lineas.append(
            f"⚠️ El turno no cabe en {nombre_prov} "
            f"(límite {_formato_miles(limite_tpm)} TPM)."
        )

    if intento_compacto and tokens_compacto:
        lineas.append(
            f"Se intentó el modo compacto, pero el prompt sigue en "
            f"~{_formato_miles(tokens_compacto)} tokens."
        )
    elif intento_compacto:
        lineas.append(
            "Se intentó el modo compacto, pero ni siquiera la versión reducida del "
            "prompt cabe en este límite."
        )
    else:
        lineas.append(
            "No hay un prompt compacto disponible para este turno "
            "(o el modo compacto está desactivado)."
        )

    lineas.append(
        "Configura Gemini o DeepSeek en Ajustes de API Keys, o usa Groq Dev "
        "(sube GROQ_TPM_LIMIT)."
    )
    return "\n".join(lineas)
