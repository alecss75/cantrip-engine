"""
debug_logger.py - Sistema Centralizado de Depuración y Telemetría para Llamadas de IA

Registra de forma ordenada, consistente y configurable cada llamada realizada a cualquier LLM:
1. Consola interactiva (modo compacto o modo depuración extendido).
2. Filtrado selectivo por tipo/contexto de llamada configurable en data/debug_config.json.
3. Aislamiento estricto por partida (PER SAVE):
   - Partidas activas -> saves/{campaña}/debug/llamadas/call_001_slug.txt,
     saves/{campaña}/debug/llm_debug.log y saves/{campaña}/debug/llm_history.jsonl.
   - Llamadas de génesis (antes de crear la save) -> se acumulan en búfer y se vuelcan
     automáticamente a la nueva partida en cuanto se crea.
   - Llamadas globales o sin partida -> logs/ temporalmente como fallback.
"""

import os
import json
import re
import unicodedata
from pathlib import Path
from datetime import datetime
from typing import Any
from colorama import Fore, init

init(autoreset=True)

from src.storage.fs_utils import get_user_data_dir, get_bundle_dir
from src.core.credentials import redactar_secretos

# Directorio de logs base (escribible) y ruta de configuración (bundle o local)
PROJECT_ROOT = get_user_data_dir()
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_PATH = (
    PROJECT_ROOT / "data" / "debug_config.json"
    if (PROJECT_ROOT / "data" / "debug_config.json").exists()
    else get_bundle_dir() / "data" / "debug_config.json"
)

# Estado global
_GLOBAL_CALL_COUNTER = 0
_CONFIG_CACHE: dict[str, Any] | None = None
_CARPETA_CAMPANA_ACTIVA: Path | None = None
_LLAMADAS_EN_CURSO: dict[int, dict[str, Any]] = {}
_BUFFER_LLAMADAS_PENDIENTES: list[dict[str, Any]] = []


def _obtener_siguiente_call_id() -> int:
    global _GLOBAL_CALL_COUNTER
    _GLOBAL_CALL_COUNTER += 1
    return _GLOBAL_CALL_COUNTER


def es_modo_debug_activo() -> bool:
    """Comprueba si el modo de depuración visual de IA está activo por entorno."""
    val = (
        os.getenv("AI_DEBUG")
        or os.getenv("DEBUG_IA")
        or os.getenv("DEBUG")
        or os.getenv("LLM_DEBUG")
        or ""
    ).strip().lower()
    return val in ("1", "true", "yes", "on", "verbose")


def _asegurar_directorio_logs():
    """Garantiza que la carpeta de logs exista."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def cargar_configuracion_debug(forzar_recarga: bool = False) -> dict[str, Any]:
    """
    Carga el archivo de configuración data/debug_config.json.
    Permite habilitar o inhabilitar el guardado de llamadas según su contexto.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not forzar_recarga:
        return _CONFIG_CACHE

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                _CONFIG_CACHE = json.load(f)
                return _CONFIG_CACHE
        except Exception:
            pass

    _CONFIG_CACHE = {
        "guardar_todas": True,
        "llamadas": {
            "default": True
        }
    }
    return _CONFIG_CACHE


def debe_guardar_llamada(contexto: str) -> bool:
    """
    Determina si una llamada con el contexto especificado debe guardarse en los archivos de log.
    Evalúa data/debug_config.json:
    - Búsqueda exacta del contexto en 'llamadas'.
    - Búsqueda insensible a mayúsculas.
    - Respaldo en 'guardar_todas' o 'default'.
    """
    cfg = cargar_configuracion_debug()
    llamadas = cfg.get("llamadas", {})

    if contexto in llamadas:
        return bool(llamadas[contexto])

    ctx_lower = (contexto or "").strip().lower()
    for k, v in llamadas.items():
        if k.strip().lower() == ctx_lower:
            return bool(v)

    if cfg.get("guardar_todas", True):
        return True

    return bool(llamadas.get("default", True))


def _generar_slug(texto: str, max_len: int = 35) -> str:
    """Genera un fragmento de nombre de archivo legible a partir de un contexto."""
    if not texto:
        return "llamada"
    s = texto.lower()
    s = unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('utf-8')
    s = re.sub(r'[^a-zA-Z0-9]+', '_', s).strip('_')
    return s[:max_len] or "llamada"


def _formatear_archivo_llamada(call_data: dict[str, Any]) -> str:
    """Formatea la llamada en un documento de texto limpio, legible y completo."""
    call_id = call_data.get("call_id", 0)
    contexto = call_data.get("contexto", "Desconocido")
    timestamp = call_data.get("timestamp_inicio", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    estado = call_data.get("status", "EN_CURSO")
    proveedor = call_data.get("proveedor_usado") or call_data.get("proveedor", "desconocido")
    modelo = call_data.get("modelo_usado") or call_data.get("modelo", "desconocido")
    intento = call_data.get("intento", 1)
    max_intentos = call_data.get("max_intentos", 1)
    latency_ms = call_data.get("latency_ms", 0.0)
    latency_s = latency_ms / 1000.0 if latency_ms else 0.0
    prompt_tokens = call_data.get("prompt_tokens", 0)
    completion_tokens = call_data.get("completion_tokens", 0)
    cached_tokens = call_data.get("cached_tokens", 0)
    total_tokens = call_data.get("total_tokens", prompt_tokens + completion_tokens)
    cost_usd = call_data.get("cost_usd", 0.0)
    cost_str = f"${cost_usd:.6f} USD" if cost_usd > 0 else "Gratis/No tarifado"
    cfg = call_data.get("generation_config")
    cfg_str = f"temp={cfg.get('temperature', 0.7)}, max_tokens={cfg.get('max_output_tokens', 2048)}" if cfg else "default"

    lineas = [
        "=" * 80,
        f"LLAMADA IA #{call_id:03d} - {contexto}",
        "=" * 80,
        f"Timestamp:     {timestamp}",
        f"Estado:        {estado}",
        f"Proveedor:     {proveedor.upper()}",
        f"Modelo:        {modelo}",
        f"Intento:       {intento}/{max_intentos}",
        f"Latencia:      {latency_s:.2f}s ({latency_ms:.1f} ms)",
        f"Tokens:        Prompt: {prompt_tokens:,} | Caché: {cached_tokens:,} | Salida: {completion_tokens:,} | Total: {total_tokens:,}",
        f"Coste Est.:    {cost_str}",
        f"Configuración: {cfg_str}",
    ]

    sys_instr = call_data.get("system_instruction")
    if sys_instr:
        lineas.extend([
            "",
            "-" * 80,
            "INSTRUCCIÓN DEL SISTEMA (SYSTEM INSTRUCTION):",
            "-" * 80,
            sys_instr.strip(),
        ])

    errores = call_data.get("errores", [])
    if errores:
        lineas.extend([
            "",
            "-" * 80,
            f"REINTENTOS / ERRORES PREVIOS ({len(errores)}):",
            "-" * 80,
        ])
        for err in errores:
            cuota_info = f" [CUOTA AGOTADA - Bloqueo {err.get('bloqueo_s')}s]" if err.get("es_cuota") else ""
            lineas.append(
                f"- Intento {err.get('intento', '?')} [{err.get('proveedor', '').upper()}]: "
                f"{err.get('tipo_error', 'Error')} - {err.get('error', '')}{cuota_info}"
            )

    ultimo_error = call_data.get("ultimo_error")
    if ultimo_error:
        lineas.extend([
            "",
            "-" * 80,
            "ERROR FINAL (FALLO TOTAL):",
            "-" * 80,
            str(ultimo_error).strip(),
        ])

    lineas.extend([
        "",
        "=" * 80,
        "PROMPT ENVIADO",
        "=" * 80,
        call_data.get("prompt", "").strip(),
        "",
        "=" * 80,
        "RESPUESTA RECIBIDA",
        "=" * 80,
        call_data.get("response_text", "").strip() if call_data.get("response_text") else "[Sin respuesta recibida]",
        "",
        "=" * 80,
    ])

    return redactar_secretos("\n".join(lineas) + "\n")


def _guardar_archivo_llamada(call_data: dict[str, Any], carpeta_base: Path | None = None) -> Path | None:
    """Guarda el archivo individual de la llamada en debug/llamadas/call_001_slug.txt."""
    contexto = call_data.get("contexto", "")
    if not debe_guardar_llamada(contexto):
        return None

    if carpeta_base is None:
        carpeta_base = _CARPETA_CAMPANA_ACTIVA

    if carpeta_base:
        dir_llamadas = carpeta_base / "debug" / "llamadas"
    else:
        dir_llamadas = LOGS_DIR / "llamadas"

    try:
        dir_llamadas.mkdir(parents=True, exist_ok=True)
        call_id = call_data.get("call_id", 0)
        slug = _generar_slug(contexto)
        nombre_archivo = f"call_{call_id:03d}_{slug}.txt"
        ruta_archivo = dir_llamadas / nombre_archivo

        contenido = _formatear_archivo_llamada(call_data)
        with open(ruta_archivo, "w", encoding="utf-8") as f:
            f.write(contenido)
        return ruta_archivo
    except Exception:
        return None


def configurar_campana(ruta_campana: Any) -> Path | None:
    """
    Establece la carpeta de la partida activa.
    Acepta tanto la carpeta directa (saves/mi_partida) como una ruta de archivo (saves/mi_partida/gamestate.json).
    Crea automáticamente los subdirectorios debug/ y debug/llamadas/ dentro de la carpeta de la partida.
    Vuelca todas las llamadas pendientes realizadas durante la génesis directamente a la nueva partida.
    """
    global _CARPETA_CAMPANA_ACTIVA, _BUFFER_LLAMADAS_PENDIENTES, _GLOBAL_CALL_COUNTER
    if not ruta_campana:
        _CARPETA_CAMPANA_ACTIVA = None
        return None

    try:
        p = Path(ruta_campana).resolve()
        if p.is_file() or p.suffix:
            p = p.parent
        p.mkdir(parents=True, exist_ok=True)
        (p / "debug").mkdir(parents=True, exist_ok=True)
        dir_llamadas = p / "debug" / "llamadas"
        dir_llamadas.mkdir(parents=True, exist_ok=True)
        _CARPETA_CAMPANA_ACTIVA = p

        # Reanudar _GLOBAL_CALL_COUNTER desde los archivos existentes si es mayor
        max_call_existente = 0
        try:
            for arch in dir_llamadas.glob("call_*.txt"):
                partes = arch.stem.split("_")
                if len(partes) >= 2 and partes[1].isdigit():
                    num = int(partes[1])
                    if num > max_call_existente:
                        max_call_existente = num
        except Exception:
            pass
        if max_call_existente > _GLOBAL_CALL_COUNTER:
            _GLOBAL_CALL_COUNTER = max_call_existente

        # Volcar llamadas pendientes de génesis acumuladas en el búfer
        if _BUFFER_LLAMADAS_PENDIENTES:
            for item in _BUFFER_LLAMADAS_PENDIENTES:
                call_data = item.get("call_data")
                texto_debug = item.get("texto_debug")
                record_json = item.get("record_json")

                if call_data:
                    _guardar_archivo_llamada(call_data, carpeta_base=_CARPETA_CAMPANA_ACTIVA)
                if texto_debug:
                    try:
                        with open(_CARPETA_CAMPANA_ACTIVA / "debug" / "llm_debug.log", "a", encoding="utf-8") as f:
                            f.write(redactar_secretos(texto_debug))
                    except Exception:
                        pass
                if record_json:
                    try:
                        with open(_CARPETA_CAMPANA_ACTIVA / "debug" / "llm_history.jsonl", "a", encoding="utf-8") as f:
                            f.write(redactar_secretos(json.dumps(record_json, ensure_ascii=False)) + "\n")
                    except Exception:
                        pass

            _BUFFER_LLAMADAS_PENDIENTES.clear()

            # Limpiar logs temporales no asignados en LOGS_DIR para mantener logs/ limpio
            try:
                gen_log = LOGS_DIR / "llm_debug.log"
                if gen_log.exists():
                    gen_log.unlink()
                gen_jsonl = LOGS_DIR / "llm_history.jsonl"
                if gen_jsonl.exists():
                    gen_jsonl.unlink()
                gen_llamadas = LOGS_DIR / "llamadas"
                if gen_llamadas.exists():
                    import shutil
                    shutil.rmtree(gen_llamadas, ignore_errors=True)
            except Exception:
                pass

    except Exception:
        _CARPETA_CAMPANA_ACTIVA = Path(ruta_campana)

    return _CARPETA_CAMPANA_ACTIVA


def obtener_carpeta_campana() -> Path | None:
    """Retorna la carpeta de la campaña activa configurada actualmente."""
    return _CARPETA_CAMPANA_ACTIVA


def _escribir_texto_debug(contenido: str, contexto: str):
    """
    Escribe texto de depuración en los archivos correspondientes:
    - Si hay partida activa: ÚNICAMENTE en saves/{partida}/debug/llm_debug.log.
    - Si no hay partida (antes de crear save): en logs/llm_debug.log temporalmente.
    """
    if not debe_guardar_llamada(contexto):
        return

    contenido_seguro = redactar_secretos(contenido)

    if _CARPETA_CAMPANA_ACTIVA:
        try:
            debug_campana = _CARPETA_CAMPANA_ACTIVA / "debug"
            debug_campana.mkdir(parents=True, exist_ok=True)
            log_campana = debug_campana / "llm_debug.log"
            with open(log_campana, "a", encoding="utf-8") as f:
                f.write(contenido_seguro)
        except Exception:
            pass
    else:
        _asegurar_directorio_logs()
        try:
            log_file = LOGS_DIR / "llm_debug.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(contenido_seguro)
        except Exception:
            pass


def _escribir_jsonl_debug(record: dict[str, Any], contexto: str):
    """
    Escribe un registro estructurado JSONL en los archivos correspondientes:
    - Si hay partida activa: ÚNICAMENTE en saves/{partida}/debug/llm_history.jsonl.
    - Si no hay partida: en logs/llm_history.jsonl.
    """
    if not debe_guardar_llamada(contexto):
        return

    linea = redactar_secretos(json.dumps(record, ensure_ascii=False)) + "\n"

    if _CARPETA_CAMPANA_ACTIVA:
        try:
            debug_campana = _CARPETA_CAMPANA_ACTIVA / "debug"
            debug_campana.mkdir(parents=True, exist_ok=True)
            jsonl_campana = debug_campana / "llm_history.jsonl"
            with open(jsonl_campana, "a", encoding="utf-8") as f:
                f.write(linea)
        except Exception:
            pass
    else:
        _asegurar_directorio_logs()
        try:
            jsonl_file = LOGS_DIR / "llm_history.jsonl"
            with open(jsonl_file, "a", encoding="utf-8") as f:
                f.write(linea)
        except Exception:
            pass


def registrar_solicitud(
    call_id: int,
    contexto: str,
    proveedor: str,
    modelo: str,
    intento: int,
    max_intentos: int,
    prompt: str,
    generation_config: dict[str, Any] | None = None,
    system_instruction: str | None = None
):
    """
    Registra el inicio de una solicitud hacia un proveedor de IA.
    """
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt_len = len(prompt or "")
    prompt_lines = len((prompt or "").splitlines())
    cfg_str = f"temp={generation_config.get('temperature', 0.7)}, max_tokens={generation_config.get('max_output_tokens', 2048)}" if generation_config else "default"

    # Salida por consola
    if es_modo_debug_activo():
        header = f"┌── [IA DEBUG #{call_id}] REQUEST ──────────────────────────────────────────────────"
        print(Fore.CYAN + header)
        print(Fore.CYAN + f"│ Timestamp   : {timestamp_str}")
        print(Fore.CYAN + f"│ Contexto    : {contexto}")
        print(Fore.CYAN + f"│ Proveedor   : {proveedor.upper()} | Modelo: {modelo} | Intento: {intento}/{max_intentos}")
        print(Fore.CYAN + f"│ Config      : {cfg_str}")
        print(Fore.CYAN + f"│ Prompt      : {prompt_len:,} caracteres ({prompt_lines} líneas)")
        if system_instruction:
            print(Fore.CYAN + f"│ SysInstr    : {len(system_instruction):,} caracteres")
        snippet = (prompt or "")[:180].replace("\n", " ").strip()
        print(Fore.CYAN + f"│ Preview     : {snippet}...")
        print(Fore.CYAN + "└" + "─" * 68)
    else:
        # Salida sobria de 1 línea
        print(Fore.CYAN + f"[IA #{call_id}] 📡 {proveedor.upper()} ({contexto}) [Intento {intento}/{max_intentos}]...")

    call_data = {
        "call_id": call_id,
        "contexto": contexto,
        "proveedor": proveedor,
        "modelo": modelo,
        "intento": intento,
        "max_intentos": max_intentos,
        "prompt": prompt,
        "generation_config": generation_config,
        "system_instruction": system_instruction,
        "timestamp_inicio": timestamp_str,
        "status": "EN_CURSO",
        "errores": []
    }
    _LLAMADAS_EN_CURSO[call_id] = call_data

    # Guardar estado inicial en archivo individual
    _guardar_archivo_llamada(call_data)

    # Registro en archivo de depuración
    texto = (
        f"\n{'='*80}\n"
        f"[{timestamp_str}] [REQUEST #{call_id}] [{contexto}]\n"
        f"Proveedor: {proveedor.upper()} | Modelo: {modelo} | Intento: {intento}/{max_intentos}\n"
        f"Config: {cfg_str}\n"
        f"Longitud Prompt: {prompt_len} caracteres\n"
        f"--- INICIO PROMPT ---\n"
        f"{prompt or ''}\n"
        f"--- FIN PROMPT ---\n"
    )
    _escribir_texto_debug(texto, contexto)

    if not _CARPETA_CAMPANA_ACTIVA and debe_guardar_llamada(contexto):
        _BUFFER_LLAMADAS_PENDIENTES.append({
            "call_data": dict(call_data),
            "texto_debug": texto,
            "record_json": None,
            "contexto": contexto
        })


def registrar_exito(
    call_id: int,
    contexto: str,
    proveedor: str,
    modelo: str,
    intento: int,
    latency_ms: float,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cost_usd: float,
    response_text: str
):
    """
    Registra una respuesta exitosa con su telemetría completa.
    """
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latency_s = latency_ms / 1000.0
    total_tokens = prompt_tokens + completion_tokens
    resp_len = len(response_text or "")

    cache_info = f" (Caché: {cached_tokens})" if cached_tokens > 0 else ""
    cost_info = f"${cost_usd:.6f} USD" if cost_usd > 0 else "Gratis/No tarifado"

    # Salida por consola
    if es_modo_debug_activo():
        header = f"┌── [IA DEBUG #{call_id}] RESPONSE OK ──────────────────────────────────────────────"
        print(Fore.GREEN + header)
        print(Fore.GREEN + f"│ Timestamp   : {timestamp_str}")
        print(Fore.GREEN + f"│ Contexto    : {contexto}")
        print(Fore.GREEN + f"│ Proveedor   : {proveedor.upper()} | Modelo: {modelo}")
        print(Fore.GREEN + f"│ Latencia    : {latency_s:.2f}s ({latency_ms:.1f} ms)")
        print(Fore.GREEN + f"│ Tokens      : In: {prompt_tokens}{cache_info} | Out: {completion_tokens} | Total: {total_tokens}")
        print(Fore.GREEN + f"│ Coste Est.  : {cost_info}")
        print(Fore.GREEN + f"│ Salida      : {resp_len:,} caracteres")
        snippet = (response_text or "")[:200].replace("\n", " ").strip()
        print(Fore.GREEN + f"│ Preview     : {snippet}...")
        print(Fore.GREEN + "└" + "─" * 68)
    else:
        # Salida sobria de 1 línea estilizada
        c_str = f" | {cost_info}" if cost_usd > 0 else ""
        ca_str = f" (Caché: {cached_tokens})" if cached_tokens > 0 else ""
        print(Fore.GREEN + f"[IA #{call_id}] ✅ {proveedor.upper()} ({modelo}) [{latency_s:.2f}s] | In: {prompt_tokens}{ca_str} | Out: {completion_tokens}{c_str}")

    call_data = _LLAMADAS_EN_CURSO.pop(call_id, {})
    call_data.update({
        "call_id": call_id,
        "contexto": contexto,
        "proveedor_usado": proveedor,
        "modelo_usado": modelo,
        "intento": intento,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "response_text": response_text,
        "status": "EXITOSO",
        "timestamp_fin": timestamp_str
    })

    # Guardar/actualizar archivo individual de la llamada
    _guardar_archivo_llamada(call_data)

    # Registro en archivo de depuración
    texto = (
        f"[{timestamp_str}] [RESPONSE #{call_id}] [OK]\n"
        f"Proveedor: {proveedor.upper()} | Modelo: {modelo} | Latencia: {latency_s:.2f}s ({latency_ms:.1f}ms)\n"
        f"Tokens: In={prompt_tokens}, Cached={cached_tokens}, Out={completion_tokens}, Total={total_tokens} | Coste: {cost_info}\n"
        f"Longitud Respuesta: {resp_len} caracteres\n"
        f"--- INICIO RESPUESTA ---\n"
        f"{response_text or ''}\n"
        f"--- FIN RESPUESTA ---\n"
        f"{'='*80}\n"
    )
    _escribir_texto_debug(texto, contexto)

    # Registro estructurado JSONL
    record = {
        "call_id": call_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "context": contexto,
        "status": "SUCCESS",
        "provider": proveedor,
        "model": modelo,
        "attempt": intento,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "response_length": resp_len
    }
    _escribir_jsonl_debug(record, contexto)

    if not _CARPETA_CAMPANA_ACTIVA and debe_guardar_llamada(contexto):
        _BUFFER_LLAMADAS_PENDIENTES.append({
            "call_data": dict(call_data),
            "texto_debug": texto,
            "record_json": record,
            "contexto": contexto
        })


def registrar_error(
    call_id: int,
    contexto: str,
    proveedor: str,
    intento: int,
    error: Exception,
    es_cuota: bool = False,
    tiempo_bloqueo_s: int | None = None
):
    """
    Registra un error o conmutación durante la llamada al LLM.
    """
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    err_str = str(error)

    if es_modo_debug_activo():
        header = f"┌── [IA DEBUG #{call_id}] ERROR / REINTENTO ─────────────────────────────────────────"
        print(Fore.YELLOW + header)
        print(Fore.YELLOW + f"│ Timestamp   : {timestamp_str}")
        print(Fore.YELLOW + f"│ Contexto    : {contexto}")
        print(Fore.YELLOW + f"│ Proveedor   : {proveedor.upper()} [Intento {intento}]")
        print(Fore.YELLOW + f"│ Tipo Error  : {type(error).__name__}")
        print(Fore.YELLOW + f"│ Detalle     : {err_str[:120]}")
        if es_cuota and tiempo_bloqueo_s:
            print(Fore.RED + f"│ [CUOTA]     : Límite excedido. Bloqueado en lista negra por {tiempo_bloqueo_s}s.")
        print(Fore.YELLOW + "└" + "─" * 68)
    else:
        if es_cuota:
            print(Fore.RED + f"[IA #{call_id}] ⚠️ Cuota agotada en {proveedor.upper()}. Bloqueado temporalmente. Conmutando...")
        else:
            print(Fore.YELLOW + f"[IA #{call_id}] ⚠️ Error en {proveedor.upper()}: {err_str[:90]}")

    if call_id in _LLAMADAS_EN_CURSO:
        _LLAMADAS_EN_CURSO[call_id].setdefault("errores", []).append({
            "timestamp": timestamp_str,
            "proveedor": proveedor,
            "intento": intento,
            "error": err_str,
            "tipo_error": type(error).__name__,
            "es_cuota": es_cuota,
            "bloqueo_s": tiempo_bloqueo_s
        })
        _guardar_archivo_llamada(_LLAMADAS_EN_CURSO[call_id])

    # Registro en archivo
    texto = (
        f"[{timestamp_str}] [ERROR #{call_id}] [Proveedor: {proveedor.upper()}] [Intento: {intento}]\n"
        f"Contexto: {contexto}\n"
        f"Tipo: {type(error).__name__}\n"
        f"Detalle: {err_str}\n"
    )
    if es_cuota:
        texto += f"Cuota agotada. Bloqueado: {tiempo_bloqueo_s}s\n"
    texto += f"{'-'*40}\n"
    _escribir_texto_debug(texto, contexto)

    # Registro JSONL
    record = {
        "call_id": call_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "context": contexto,
        "status": "ERROR_RETRY",
        "provider": proveedor,
        "attempt": intento,
        "is_quota": es_cuota,
        "error_type": type(error).__name__,
        "error_message": err_str[:250]
    }
    _escribir_jsonl_debug(record, contexto)


def registrar_fallo_total(call_id: int, contexto: str, ultimo_error: Exception | None):
    """
    Registra el agotamiento absoluto de todos los proveedores configurados.
    """
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if es_modo_debug_activo():
        header = f"┌── [IA DEBUG #{call_id}] FALLO TOTAL ──────────────────────────────────────────────"
        print(Fore.RED + header)
        print(Fore.RED + f"│ Contexto    : {contexto}")
        print(Fore.RED + "│ Detalle     : Todos los proveedores disponibles fallaron.")
        print(Fore.RED + f"│ Último error: {str(ultimo_error)[:150]}")
        print(Fore.RED + "└" + "─" * 68)
    else:
        print(Fore.RED + f"[IA #{call_id}] ❌ Fallaron todos los proveedores de IA ({contexto}). Último error: {ultimo_error}")

    call_data = _LLAMADAS_EN_CURSO.pop(call_id, {})
    call_data.update({
        "call_id": call_id,
        "contexto": contexto,
        "status": "FALLO_TOTAL",
        "ultimo_error": str(ultimo_error),
        "timestamp_fin": timestamp_str
    })
    _guardar_archivo_llamada(call_data)

    texto = (
        f"[{timestamp_str}] [FALLO TOTAL #{call_id}] Contexto: {contexto}\n"
        f"Ultimo error: {str(ultimo_error)}\n"
        f"{'='*80}\n"
    )
    _escribir_texto_debug(texto, contexto)

    record = {
        "call_id": call_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "context": contexto,
        "status": "TOTAL_FAILURE",
        "last_error": str(ultimo_error)
    }
    _escribir_jsonl_debug(record, contexto)

    if not _CARPETA_CAMPANA_ACTIVA and debe_guardar_llamada(contexto):
        _BUFFER_LLAMADAS_PENDIENTES.append({
            "call_data": dict(call_data),
            "texto_debug": texto,
            "record_json": record,
            "contexto": contexto
        })
