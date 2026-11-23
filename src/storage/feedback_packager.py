"""
feedback_packager.py - Empaquetador Sanitizado de Partidas ("Ayuda a Mejorar el Motor"),
Reporte de Bugs y Enlaces de Apoyo (Ko-fi)

Permite a los jugadores:
1. Empaquetar de forma voluntaria una campaña (`saves/{campaña}/` + `debug/` + `logs/`)
   en un archivo `.zip` sanitizado (sin API Keys, rutas absolutas del SO ni binarios ajenos)
   siguiendo una allowlist estricta para compartirla y auditarla. La semilla original
   se conserva para diagnóstico; el resto de prompts sigue censurado.
2. Generar reportes de bugs con diagnóstico técnico hacia
   `alejandrosansoresarjona@gmail.com` (compatible con Gmail Web, `mailto:` y portapapeles).
3. Acceder al canal de apoyo del proyecto en Ko-fi (`https://ko-fi.com/alecss75`).
"""

from __future__ import annotations

import os
import sys
import json
import re
import platform
import zipfile
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
from typing import Dict, Any, List, Optional

from src.storage.fs_utils import get_user_data_dir, get_build_info, sanitize_filename
from src.core.credentials import redactar_secretos, obtener_proveedor_primario, resumen_estado_proveedores

DEVELOPER_EMAIL = "alejandrosansoresarjona@gmail.com"
KOFI_URL = "https://ko-fi.com/alecss75"

EXTENSIONES_TEXTO_PERMITIDAS = {
    ".json", ".jsonl", ".txt", ".log", ".md", ".csv", ".ini", ".cfg"
}

ARCHIVOS_RAIZ_PERMITIDOS = {
    "gamestate.json",
    "character.json",
    "world_map.json",
    "world_bible.json",
    "npc_index.json",
    "history.jsonl",
    "consultas_dm.jsonl",
    "bitacora.md",
    "cronica.md",
}


def _limpiar_campo(texto: Any, una_linea: bool = False) -> str:
    """Sanitiza secretos y rutas, y opcionalmente colapsa a una única línea para asuntos/headers."""
    s = str(texto or "").strip()
    limpio = redactar_secretos(s, anonimizar_rutas=True)
    if una_linea:
        limpio = re.sub(r"[\r\n\t]+", " ", limpio).strip()
    return limpio


def _extraer_semilla(carpeta: Path, gs_data: Optional[dict] = None) -> str:
    """Recupera la semilla original de una campaña para el reporte de feedback.

    Prioriza `gamestate.meta.concepto_semilla` (campañas nuevas). Si no está,
    la busca en los prompts crudos de `debug/llamadas/` para que las partidas
    antiguas también la incluyan. No se anonimiza el texto de la semilla; solo
    se sanitizan secretos y rutas al incorporarla al zip.
    """
    gs_data = gs_data if isinstance(gs_data, dict) else {}
    meta = gs_data.get("meta") or {}
    semilla = str(meta.get("concepto_semilla") or "").strip() if isinstance(meta, dict) else ""
    if semilla:
        return semilla

    llamadas = carpeta / "debug" / "llamadas"
    if not llamadas.is_dir():
        return ""

    patrones = (
        r"Semilla original:\s*\"([^\"\n]+)\"",
        r"Deconstruye el siguiente concepto libre[^\n]*\n\s*\"([^\"\n]+)\"",
    )
    for ruta in sorted(llamadas.glob("call_*")):
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for patron in patrones:
            coincidencia = re.search(patron, texto, flags=re.IGNORECASE)
            if coincidencia:
                return coincidencia.group(1).strip()
    return ""


def anonimizar_prompts_sistema(
    contenido: str, rel_posix: str = "", semilla: str = ""
) -> str:
    """
    Anonimiza y censura las directivas maestras del DM, plantillas de prompts
    y protocolos internos de ingeniería de IA en archivos de depuración y logs
    al empaquetar una partida para feedback o reporte de bugs.

    Garantiza la protección de la propiedad intelectual (IP) del motor:
    - Oculta las directivas del sistema, reglamento D&D 5e del prompt y catálogo de intenciones.
    - Preserva la acción del usuario (<user_action>), datos de telemetría, respuestas del LLM
      y la semilla original (entrada del jugador) para posibilitar el diagnóstico técnico.
    """
    if not contenido:
        return contenido

    res = contenido
    semilla = str(semilla or "").strip()

    def _bloque_semilla(prompt_body: str) -> str:
        """Devuelve la semilla marcada para el log si el prompt la contiene."""
        if not semilla:
            return ""
        marcadores = (
            "Semilla original",
            "Deconstruye el siguiente concepto",
            "Concepto Semilla",
        )
        if semilla in prompt_body or any(m in prompt_body for m in marcadores):
            return f"\n<semilla_original>{semilla}</semilla_original>\n"
        return ""

    # 1. Archivos individuales de llamadas: call_*.txt
    # Censura de la sección INSTRUCCIÓN DEL SISTEMA (SYSTEM INSTRUCTION)
    res = re.sub(
        r"(-{20,}\s*[\r\n]+.*(?:SYSTEM INSTRUCTION|INSTRUCCI).*[\r\n]+-{20,}\s*[\r\n]+)(?:[\s\S]*?)(?=[\r\n]+-{20,}|[\r\n]+={20,}|\Z)",
        r"\1[REDACTADO: Directivas Maestras del DM y Reglas del Sistema protegidas por Propiedad Intelectual]\n",
        res,
        flags=re.IGNORECASE,
    )

    # Censura de la sección PROMPT ENVIADO en call_*.txt
    def _redactar_prompt_enviado(match: re.Match) -> str:
        header = match.group(1)
        prompt_body = match.group(2)
        bloque_semilla = _bloque_semilla(prompt_body)
        ua_match = re.search(r"<user_action>([\s\S]*?)</user_action>", prompt_body, flags=re.IGNORECASE)
        if ua_match:
            accion_preservada = f"\n<user_action>{ua_match.group(1).strip()}</user_action>\n"
            return f"{header}[REDACTADO: Directivas del Motor y Catálogo de Reglas protegidos por Propiedad Intelectual]\n{bloque_semilla}{accion_preservada}\n"
        return f"{header}[REDACTADO: Plantilla de Prompt e Instrucciones del Motor protegidas por Propiedad Intelectual]\n{bloque_semilla}\n"

    res = re.sub(
        r"(={20,}\s*[\r\n]+.*PROMPT ENVIADO.*[\r\n]+={20,}\s*[\r\n]+)([\s\S]*?)(?=[\r\n]+={20,}\s*[\r\n]+.*RESPUESTA RECIBIDA|\Z)",
        _redactar_prompt_enviado,
        res,
        flags=re.IGNORECASE,
    )

    # 2. Bloques en llm_debug.log: --- INICIO PROMPT --- ... --- FIN PROMPT ---
    def _redactar_inicio_fin_prompt(match: re.Match) -> str:
        prompt_body = match.group(1)
        bloque_semilla = _bloque_semilla(prompt_body)
        ua_match = re.search(r"<user_action>([\s\S]*?)</user_action>", prompt_body, flags=re.IGNORECASE)
        if ua_match:
            accion_preservada = f"\n<user_action>{ua_match.group(1).strip()}</user_action>\n"
            return f"--- INICIO PROMPT ---\n[REDACTADO: Directivas del Motor protegidas por Propiedad Intelectual]{bloque_semilla}{accion_preservada}--- FIN PROMPT ---"
        return f"--- INICIO PROMPT ---\n[REDACTADO: Directivas del Motor protegidas por Propiedad Intelectual]{bloque_semilla}--- FIN PROMPT ---"

    res = re.sub(
        r"--- INICIO PROMPT ---\s*[\r\n]+([\s\S]*?)[\r\n]+--- FIN PROMPT ---",
        _redactar_inicio_fin_prompt,
        res,
        flags=re.IGNORECASE,
    )

    # 3. Barrido de seguridad general ante fragmentos sueltos de directivas maestras
    patrones_clave = [
        r"ROL DEL DUNGEON MASTER[\s\S]*?(?=<user_action>|###JSON_STOP###|\Z)",
        r"CONTEXTO GENERAL DE SEGURIDAD[\s\S]*?(?=<user_action>|###JSON_STOP###|\Z)",
        r"DIRECTIVA DE SEGURIDAD CONTRA INYECCIONES[\s\S]*?(?=<user_action>|###JSON_STOP###|\Z)",
        r"PROTOCOLO MEC[AÁ]NICO OBLIGATORIO[\s\S]*?(?=<user_action>|###JSON_STOP###|\Z)",
        r"PRINCIPIOS FUNDAMENTALES[\s\S]*?(?=<user_action>|###JSON_STOP###|\Z)",
    ]
    for pat in patrones_clave:
        res = re.sub(pat, "[REDACTADO: Instrucciones del Motor protegidas]\n", res, flags=re.IGNORECASE)

    return res


def obtener_ultimas_lineas_log(max_lineas: int = 250) -> str:
    """Lee y sanitiza las últimas N líneas de `logs/cantrip.log`."""
    log_path = get_user_data_dir() / "logs" / "cantrip.log"
    if not log_path.exists():
        return "[Archivo logs/cantrip.log aún no generado en esta sesión]"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lineas = f.readlines()
        recorte = "".join(lineas[-max_lineas:])
        return redactar_secretos(recorte, anonimizar_rutas=True)
    except Exception as e:
        return f"[No se pudo leer cantrip.log: {e}]"


def _resolver_carpeta_campana(ruta_campana: str | Path) -> Path:
    """Resuelve tanto el nombre de una campaña como la ruta a su carpeta o a su `gamestate.json`."""
    p = Path(ruta_campana)
    if not p.exists():
        candidato = get_user_data_dir() / "saves" / str(ruta_campana)
        if candidato.exists():
            p = candidato
    p = p.resolve()
    if p.is_file() or p.suffix == ".json":
        p = p.parent
    return p


def _es_archivo_permitido(rel_posix: str) -> bool:
    """Valida si una ruta relativa pertenece a la allowlist autorizada de juego y debug."""
    partes = rel_posix.split("/")
    nombre_archivo = partes[-1]
    ext = Path(nombre_archivo).suffix.lower()

    if ext not in EXTENSIONES_TEXTO_PERMITIDAS:
        return False

    # Archivos permitidos en raíz de la campaña
    if len(partes) == 1:
        return nombre_archivo in ARCHIVOS_RAIZ_PERMITIDOS

    # Subcarpetas permitidas
    primer_dir = partes[0]
    if primer_dir == "npcs" and len(partes) == 3 and partes[1] in ("active", "archived"):
        return True
    if primer_dir == "debug":
        return True

    return False


def empaquetar_campana_para_mejora(
    ruta_campana: str | Path,
    comentario_jugador: str = "",
    valoracion: str = "5/5 - ¡Gran aventura!",
    directorio_salida: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Empaqueta una campaña (`saves/{uuid}/` + `debug/` + extracto de `logs/`)
    en un archivo `.zip` aplicando allowlist y sanitización estricta.

    Garantía de Privacidad:
    - Todos los archivos incluidos se sanitizan en memoria con `redactar_secretos(..., anonimizar_rutas=True)`.
    - La semilla original se conserva legible (también pasa por la redacción de secretos/rutas)
      porque explica cómo se forjó la campaña; el resto de prompts sigue censurado.
    - No se empaquetan binarios, `.env` ni archivos ajenos no contemplados en la allowlist.
    - Se genera un MANIFEST.json interno con la lista de archivos incluidos y omitidos.
    - No se exponen rutas locales del disco en el asunto ni en el cuerpo del correo.
    """
    carpeta = _resolver_carpeta_campana(ruta_campana)
    if not carpeta.exists() or not carpeta.is_dir():
        raise FileNotFoundError(f"No se encontró la carpeta de la campaña: {carpeta}")

    # Extraer título amigable de gamestate.json si existe
    titulo_campana_raw = carpeta.name
    turnos_jugados = 0
    heroe_info_raw = "Desconocido"
    gs_data: dict = {}
    gs_path = carpeta / "gamestate.json"
    if gs_path.exists():
        try:
            with open(gs_path, "r", encoding="utf-8-sig") as f:
                gs_data = json.load(f)
            if not isinstance(gs_data, dict):
                gs_data = {}
            titulo_campana_raw = gs_data.get("meta", {}).get("titulo", carpeta.name)
            turnos_jugados = int(gs_data.get("historial", {}).get("turnos_totales", 0))
            pj = gs_data.get("personaje", {})
            heroe_info_raw = f"{pj.get('nombre', 'Héroe')} ({pj.get('clase', 'Aventurero')} Nvl {pj.get('nivel', 1)})"
        except Exception:
            pass

    titulo_campana = _limpiar_campo(titulo_campana_raw, una_linea=True)
    heroe_info = _limpiar_campo(heroe_info_raw, una_linea=True)
    valoracion_limpia = _limpiar_campo(valoracion, una_linea=True)
    comentario_limpio = _limpiar_campo(comentario_jugador)
    # La semilla es la entrada que explica la campaña: se conserva en el reporte
    # (solo se redactan secretos y rutas), a diferencia del resto de prompts.
    semilla_limpia = _limpiar_campo(_extraer_semilla(carpeta, gs_data), una_linea=True)

    slug = sanitize_filename(titulo_campana)[:40]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if directorio_salida:
        out_dir = Path(directorio_salida).resolve()
    else:
        out_dir = get_user_data_dir() / "exports" / "feedback"
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_filename = f"aporte_motor_{slug}_{timestamp}.zip"
    zip_path = out_dir / zip_filename

    archivos_incluidos: List[str] = []
    archivos_omitidos: List[str] = []
    build_info = get_build_info()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Empaquetar archivos de la campaña siguiendo la allowlist
        for root, _, files in os.walk(carpeta):
            for file in sorted(files):
                # Omitir temporales
                if file.startswith("tmp_save_") or file.endswith(".tmp"):
                    continue
                full_p = Path(root) / file
                rel_posix = full_p.relative_to(carpeta).as_posix()

                if not _es_archivo_permitido(rel_posix):
                    archivos_omitidos.append(rel_posix)
                    continue

                arcname = f"save/{rel_posix}"
                try:
                    raw_txt = full_p.read_text(encoding="utf-8", errors="replace")
                    clean_txt = redactar_secretos(raw_txt, anonimizar_rutas=True)
                    if rel_posix.startswith("debug/") or "log" in rel_posix or rel_posix.endswith((".txt", ".log", ".jsonl")):
                        clean_txt = anonimizar_prompts_sistema(
                            clean_txt, rel_posix, semilla=semilla_limpia
                        )
                    zf.writestr(arcname, clean_txt.encode("utf-8"))
                    archivos_incluidos.append(arcname)
                except Exception:
                    archivos_omitidos.append(rel_posix)

        # 2. Extracto sanitizado de log general
        log_extracto = obtener_ultimas_lineas_log(max_lineas=400)
        log_extracto = anonimizar_prompts_sistema(
            log_extracto, "cantrip_tail.log", semilla=semilla_limpia
        )
        zf.writestr("logs/cantrip_tail.log", log_extracto.encode("utf-8"))
        archivos_incluidos.append("logs/cantrip_tail.log")

        # 3. Semilla original aislada, para localizarla rápido en el reporte.
        zf.writestr(
            "SEMILLA_ORIGINAL.txt",
            ((semilla_limpia or "(No disponible en esta partida)") + "\n").encode("utf-8"),
        )
        archivos_incluidos.append("SEMILLA_ORIGINAL.txt")

        # 4. NOTAS_DEL_JUGADOR.txt
        notas_contenido = (
            "========================================================================\n"
            "APORTE VOLUNTARIO PARA MEJORAR EL MOTOR - CANTRIP ENGINE\n"
            "========================================================================\n"
            f"Fecha de Empaquetado : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Campaña              : {titulo_campana}\n"
            f"Semilla Original     : {semilla_limpia or '(No disponible en esta partida)'}\n"
            f"Personaje            : {heroe_info}\n"
            f"Turnos Jugados       : {turnos_jugados}\n"
            f"Valoración           : {valoracion_limpia}\n"
            f"Versión del Build    : {build_info.get('version')} ({build_info.get('git_commit')})\n"
            f"Sistema Operativo    : {platform.system()} {platform.release()}\n"
            f"Proveedor Primario   : {obtener_proveedor_primario().upper()}\n"
            "Garantía Privacidad  : Secretos y rutas sanitizados; semilla original preservada para diagnóstico.\n"
            "------------------------------------------------------------------------\n"
            "COMENTARIOS / OBSERVACIONES DEL JUGADOR:\n"
            "------------------------------------------------------------------------\n"
            f"{comentario_limpio or '(Sin comentarios adicionales)'}\n"
            "========================================================================\n"
        )
        zf.writestr("NOTAS_DEL_JUGADOR.txt", notas_contenido.encode("utf-8"))
        archivos_incluidos.append("NOTAS_DEL_JUGADOR.txt")

        # 5. MANIFEST.json de auditoría
        manifest_data = {
            "version": build_info.get("version"),
            "commit": build_info.get("git_commit"),
            "generado_en": datetime.now().isoformat(),
            "semilla_original": semilla_limpia or None,
            "archivos_incluidos": archivos_incluidos,
            "archivos_omitidos": archivos_omitidos,
        }
        zf.writestr("MANIFEST.json", json.dumps(manifest_data, indent=2, ensure_ascii=False).encode("utf-8"))
        archivos_incluidos.append("MANIFEST.json")

    asunto = f"[Aporte Motor D&D] Partida y Logs: {titulo_campana[:60]} ({turnos_jugados} turnos)"
    cuerpo_correo = (
        f"¡Hola Alejandro!\n\n"
        f"Te mando mi partida '{titulo_campana}' ({heroe_info}, {turnos_jugados} turnos) "
        f"para que puedas ver cómo se portó el motor.\n\n"
        f"¿Cómo estuvo?: {valoracion_limpia}\n"
        f"Notas / comentarios:\n"
        f"{comentario_limpio or '(Sin comentarios extra)'}\n\n"
        f"📎 Adjunto a este correo el archivo .zip que armó el juego:\n"
        f"   -> {zip_path.name}\n"
    )

    mailto_url = f"mailto:{DEVELOPER_EMAIL}?subject={quote(asunto)}&body={quote(cuerpo_correo)}"
    gmail_web_url = (
        f"https://mail.google.com/mail/?view=cm&fs=1"
        f"&to={quote(DEVELOPER_EMAIL)}&su={quote(asunto)}&body={quote(cuerpo_correo)}"
    )

    return {
        "zip_path": str(zip_path),
        "archivos_incluidos": archivos_incluidos,
        "archivos_omitidos": archivos_omitidos,
        "titulo_campana": titulo_campana,
        "turnos_jugados": turnos_jugados,
        "asunto": asunto,
        "cuerpo_correo": cuerpo_correo,
        "mailto_url": mailto_url,
        "gmail_web_url": gmail_web_url,
    }


def construir_reporte_bug(
    descripcion: str,
    categoria: str = "Error General / Mecánicas",
    pasos_reproducir: str = "",
    ruta_campana: Optional[str | Path] = None,
    incluir_logs: bool = True,
) -> Dict[str, str]:
    """
    Construye un reporte estructurado y sanitizado de Bug/Error listo para enviar a
    `alejandrosansoresarjona@gmail.com` vía Gmail Web, cliente de correo local o portapapeles.
    """
    build_info = get_build_info()
    prov_activo = obtener_proveedor_primario().upper()
    proveedores_cfg = [
        k.upper() for k, v in resumen_estado_proveedores().items() if v.get("configurada") == "si"
    ]

    campana_str_raw = "Ninguna (Menú Principal)"
    if ruta_campana:
        try:
            carp = _resolver_carpeta_campana(ruta_campana)
            campana_str_raw = carp.name
        except Exception:
            campana_str_raw = str(ruta_campana)

    extracto_log = ""
    if incluir_logs:
        extracto_log = obtener_ultimas_lineas_log(max_lineas=45)
        extracto_log = anonimizar_prompts_sistema(extracto_log, "cantrip.log")

    desc_limpia = _limpiar_campo(descripcion or "(Sin descripción)")
    pasos_limpios = _limpiar_campo(pasos_reproducir or "(No especificados)")
    cat_limpia = _limpiar_campo(categoria, una_linea=True)
    campana_limpia = _limpiar_campo(campana_str_raw, una_linea=True)

    desc_asunto = re.sub(r"[\r\n\t]+", " ", desc_limpia).strip()[:55]
    asunto = f"[Bug Report D&D RPG] [{cat_limpia}] {desc_asunto}"

    cuerpo_completo = (
        "====================================================================\n"
        "REPORTE DE BUG / ANOMALÍA - CANTRIP ENGINE\n"
        "====================================================================\n"
        f"Categoría          : {cat_limpia}\n"
        f"Fecha              : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Versión / Build    : v{build_info.get('version')} ({build_info.get('channel')} • commit {build_info.get('git_commit')})\n"
        f"Sistema Operativo  : {platform.system()} {platform.release()} (Python {sys.version.split()[0]})\n"
        f"Proveedor Primario : {prov_activo} (Configurados: {', '.join(proveedores_cfg) or 'Ninguno'})\n"
        f"Partida Activa     : {campana_limpia}\n"
        "--------------------------------------------------------------------\n"
        "1. ¿QUÉ OCURRIÓ?\n"
        f"{desc_limpia}\n\n"
        "2. PASOS O ACCIÓN REALIZADA ANTES DEL ERROR:\n"
        f"{pasos_limpios}\n"
    )

    if incluir_logs and extracto_log:
        cuerpo_completo += (
            "--------------------------------------------------------------------\n"
            "3. REGISTRO RECIENTE SANITIZADO (logs/cantrip.log):\n"
            f"{extracto_log[-1800:]}\n"
        )
    cuerpo_completo += "====================================================================\n"

    cuerpo_url = cuerpo_completo[:1600]
    mailto_url = f"mailto:{DEVELOPER_EMAIL}?subject={quote(asunto)}&body={quote(cuerpo_url)}"
    gmail_web_url = (
        f"https://mail.google.com/mail/?view=cm&fs=1"
        f"&to={quote(DEVELOPER_EMAIL)}&su={quote(asunto)}&body={quote(cuerpo_url)}"
    )

    return {
        "destinatario": DEVELOPER_EMAIL,
        "asunto": asunto,
        "cuerpo": cuerpo_completo,
        "mailto_url": mailto_url,
        "gmail_web_url": gmail_web_url,
    }
