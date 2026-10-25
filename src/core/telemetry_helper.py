"""
telemetry_helper.py - Módulo centralizado para la acumulación y auditoría de telemetría IA.

Garantiza que todas las llamadas realizadas al gateway (génesis de campaña, turnos principales,
segundas llamadas de desenlace de dados, y resúmenes de memoria o cierre de sesión)
queden reflejadas con absoluta precisión aditiva en 'telemetria_ia' dentro de gamestate.json.
"""

from typing import Any

_BUFFER_TELEMETRIA_PENDIENTE: list[Any] = []


def registrar_llamada_huerfana(resp: Any) -> None:
    """Registra una respuesta LLM ejecutada antes de crear gamestate (génesis de campaña)."""
    if resp is not None:
        _BUFFER_TELEMETRIA_PENDIENTE.append(resp)


def vaciar_buffer_en_estado(estado: dict[str, Any]) -> None:
    """Vuelca todas las llamadas del buffer dentro de la telemetría del gamestate."""
    global _BUFFER_TELEMETRIA_PENDIENTE
    for r in _BUFFER_TELEMETRIA_PENDIENTE:
        acumular_telemetria(estado, r, es_turno_jugador=False)
    _BUFFER_TELEMETRIA_PENDIENTE.clear()


def acumular_telemetria(
    estado: dict[str, Any],
    resp: Any | None,
    es_turno_jugador: bool = False
) -> None:
    """
    Acumula de forma consistente los tokens, costes y proveedor de cualquier llamada LLM.
    
    Args:
        estado: Diccionario del estado del juego (gamestate).
        resp: Objeto LLMResponse devuelto por el gateway (o None si falló).
        es_turno_jugador: Si es True, incrementa el contador de turnos de jugador.
    """
    if not resp or not isinstance(estado, dict):
        return

    telemetria = estado.setdefault("telemetria_ia", {
        "turnos_ejecutados": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cached_tokens": 0,
        "total_coste_usd": 0.0,
        "ultimo_proveedor": "",
        "ultimo_modelo": ""
    })

    if es_turno_jugador:
        telemetria["turnos_ejecutados"] = int(telemetria.get("turnos_ejecutados", 0)) + 1

    prompt_tok = getattr(resp, "prompt_tokens", 0) or 0
    comp_tok = getattr(resp, "completion_tokens", 0) or 0
    cached_tok = getattr(resp, "cached_tokens", 0) or 0
    cost_usd = getattr(resp, "cost_usd", 0.0) or 0.0
    prov = getattr(resp, "provider_used", "") or telemetria.get("ultimo_proveedor", "")
    model = getattr(resp, "model_used", "") or telemetria.get("ultimo_modelo", "")

    telemetria["total_prompt_tokens"] = int(telemetria.get("total_prompt_tokens", 0)) + prompt_tok
    telemetria["total_completion_tokens"] = int(telemetria.get("total_completion_tokens", 0)) + comp_tok
    telemetria["total_cached_tokens"] = int(telemetria.get("total_cached_tokens", 0)) + cached_tok
    telemetria["total_coste_usd"] = round(float(telemetria.get("total_coste_usd", 0.0)) + float(cost_usd), 6)

    if prov:
        telemetria["ultimo_proveedor"] = prov
    if model:
        telemetria["ultimo_modelo"] = model
