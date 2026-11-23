"""
store.py - Capa de Persistencia Atómica y Transaccional

Garantiza integridad de datos ante caídas de proceso, cortes de luz o
bloqueos temporales de sincronización (OneDrive/Dropbox):
1. Escritura atómica vía archivo temporal en el mismo volumen + os.replace.
2. Rotación automática de respaldos (.bak1, .bak2, .bak3).
3. Recuperación automática desde respaldo ante corrupción de archivo.
4. Historial transaccional en JSON Lines (history.jsonl).
5. Consultas OOC al DM en JSON Lines (consultas_dm.jsonl), separadas del
   historial de turnos para no contaminar crónica, PDF ni prompts.
6. Admite indistintamente rutas de archivo JSON o carpetas de campaña.
"""

import json
import logging
import os
import shutil
import tempfile
import datetime
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("cantrip.store")

#: Consultas fuera de personaje («Pregúntale al DM»). Un registro por línea.
CONSULTAS_DM_FILE = "consultas_dm.jsonl"


def _resolve_path(target_path: Path | str) -> Path:
    """Ancla rutas relativas a la raíz de datos del jugador, nunca al CWD.

    Import local para evitar un ciclo de importación: ``src.core`` importa
    ``src.storage.store`` durante la carga de la aplicación.
    """
    from src.core.paths import resolve_data_path

    return resolve_data_path(target_path)


def save_atomic(target_path: Path | str, data: Dict[str, Any], backups: int = 3) -> bool:
    """
    Guarda un diccionario en formato JSON de forma atómica.
    Acepta tanto una ruta directa al archivo .json como a la carpeta de la campaña.
    Las rutas relativas se resuelven contra la raíz de datos (no contra el CWD).
    
    1. Rota copias de seguridad existentes (.bak1, .bak2...).
    2. Escribe en un archivo temporal en la misma carpeta.
    3. Realiza un flush + fsync a disco físico.
    4. Reemplaza el archivo destino atómicamente con os.replace.
    """
    path = _resolve_path(target_path)
    if path.is_dir() or not path.suffix:
        path = path / "gamestate.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Rotación de backups si el archivo ya existe y es válido
    if path.exists() and path.is_file() and backups > 0:
        try:
            for i in range(backups - 1, 0, -1):
                src = path.with_suffix(f".json.bak{i}")
                dst = path.with_suffix(f".json.bak{i+1}")
                if src.exists():
                    shutil.copy2(src, dst)
            shutil.copy2(path, path.with_suffix(".json.bak1"))
        except Exception as e:
            # Si falla la rotación de backups, no bloqueamos el guardado pero lo registramos
            logger.warning("No se pudo rotar backup de %s: %s", path.name, e)

    # 2. Escritura atómica vía archivo temporal en la misma carpeta
    temp_fd, temp_file = tempfile.mkstemp(dir=path.parent, prefix="tmp_save_", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 3. Reemplazo atómico
        os.replace(temp_file, path)
        return True
    except Exception as e:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
        raise IOError(f"Error crítico en guardado atómico de {path}: {e}")


def _migrar_esquema_si_procede(data: Any, path: Path) -> Any:
    """
    Aplica la compuerta de migración solo a `gamestate.json`.

    El resto de artefactos (`world_map.json`, `npc_index.json`, etc.) no
    llevan versión de esquema y no deben reescribirse al cargarse.
    """
    if path.name != "gamestate.json" or not isinstance(data, dict):
        return data

    # Import local: save_migrations importa save_atomic de este módulo.
    from src.storage.save_migrations import SaveTooOld, migrar_esquema

    try:
        data = migrar_esquema(data, path)
    except SaveTooOld:
        raise
    except Exception as exc:  # noqa: BLE001 - un save legible nunca debe dejar de cargar
        logger.warning("No se pudo migrar el esquema de %s: %s", path.name, exc)

    # Sanea rutas heredadas (p. ej. ``saves\\Mi Campaña``) para que los
    # consumidores de meta.carpeta no dependan del CWD del proceso.
    from src.core.paths import bind_campaign_state

    return bind_campaign_state(data, path.parent)


def load_json_safe(target_path: Path | str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Carga un archivo JSON de forma segura.
    Si el archivo está dañado o truncado, intenta recuperarse desde el backup más reciente.
    Si no hay backup viable, devuelve el valor default o lanza un error.

    Para `gamestate.json` aplica la compuerta de migración de esquema
    (`src.storage.save_migrations`); puede lanzar `SaveTooOld`.
    """
    path = _resolve_path(target_path)
    if path.is_dir() or not path.suffix:
        path = path / "gamestate.json"

    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Archivo no encontrado: {path}")

    # Intentar leer archivo principal.
    # Se usa utf-8-sig para tolerar un BOM UTF-8 (frecuente en archivos
    # editados o generados por herramientas externas como PowerShell/Editores),
    # sin el cual json.load fallaría y la campaña caería a estado por defecto.
    #
    # El handle se cierra antes de migrar: en Windows, `os.replace` falla si el
    # archivo destino sigue abierto por este mismo proceso.
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Archivo corrupto detectado en %s: %s", path, e)
        # Intentar restaurar desde backup
        for i in range(1, 4):
            bak_path = path.with_suffix(f".json.bak{i}")
            if bak_path.exists():
                try:
                    with open(bak_path, 'r', encoding='utf-8-sig') as bf:
                        recovered = json.load(bf)
                    logger.info("Recuperado exitosamente desde %s", bak_path.name)
                    # Restaurar archivo principal con el backup sano
                    save_atomic(path, recovered, backups=0)
                    return _migrar_esquema_si_procede(recovered, path)
                except Exception:
                    continue

        if default is not None:
            logger.warning("No se pudo recuperar ningún backup de %s. Usando estado por defecto.", path)
            return default
        raise IOError(f"Archivo {path} y todos sus respaldos están corruptos.")

    return _migrar_esquema_si_procede(data, path)


# Alias de compatibilidad
load_with_recovery = load_json_safe


def append_history_log(
    campaign_folder: Path | str,
    entry_or_action: Any,
    narrativa_dm: Optional[str] = None,
    metadatos: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Añade una entrada al historial de la campaña en formato JSON Lines (history.jsonl).
    Garantiza que el log de turnos sobreviva de forma incremental e inmutable.
    Acepta tanto un diccionario directo como argumentos separados (accion, narrativa, metadatos).
    """
    folder = _resolve_path(campaign_folder)
    if folder.is_file() or folder.suffix:
        folder = folder.parent
    folder.mkdir(parents=True, exist_ok=True)
    log_file = folder / "history.jsonl"

    if isinstance(entry_or_action, dict):
        record = entry_or_action
    else:
        record = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "accion": entry_or_action,
            "narrativa": narrativa_dm or "",
            "metadatos": metadatos or {}
        }

    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        return True
    except Exception as e:
        logger.error("Error al escribir historial en %s: %s", log_file, e)
        return False


def load_history_log(campaign_folder: Path | str) -> list[Dict[str, Any]]:
    """Carga todas las entradas del historial de la campaña."""
    folder = _resolve_path(campaign_folder)
    if folder.is_file() or folder.suffix:
        folder = folder.parent
    log_file = folder / "history.jsonl"
    if not log_file.exists():
        return []

    entries = []
    with open(log_file, 'r', encoding='utf-8-sig') as f:
        for line in f:
            line_str = line.strip()
            if line_str:
                try:
                    entries.append(json.loads(line_str))
                except Exception:
                    continue
    return entries


# ---------------------------------------------------------------------------
# Consultas OOC al DM (panel «Pregúntale al DM» y /dm de consola)
# ---------------------------------------------------------------------------

def _carpeta_de_campana(target: Path | str) -> Path:
    """Resuelve la carpeta de campaña a partir de la carpeta o de gamestate.json."""
    folder = _resolve_path(target)
    if folder.is_file() or folder.suffix:
        folder = folder.parent
    return folder


def _ahora_utc_iso() -> str:
    """Timestamp UTC estable y legible (``2026-11-11T19:31:02Z``)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def append_consulta_dm(
    campaign_folder: Path | str,
    pregunta: str,
    respuesta: str,
    *,
    cambios: Optional[list] = None,
    aplicado: bool = True,
    turno: int = 0,
    ubicacion: str = "",
    origen: str = "motor",
) -> bool:
    """
    Registra una consulta fuera de personaje en ``consultas_dm.jsonl``.

    Es un archivo aparte de ``history.jsonl`` a propósito: la consulta no es un
    turno, no debe entrar en la crónica/PDF ni en el contexto del DM. ``aplicado``
    indica si los cambios de ficha propuestos llegaron a persistirse (en el panel
    puede ser ``False`` si la partida avanzó mientras el DM respondía).
    """
    folder = _carpeta_de_campana(campaign_folder)
    registro = {
        "v": 1,
        "ts": _ahora_utc_iso(),
        "origen": str(origen or "motor"),
        "turno": int(turno or 0),
        "ubicacion": str(ubicacion or ""),
        "pregunta": str(pregunta or "").strip(),
        "respuesta": str(respuesta or ""),
        "cambios": [str(c) for c in (cambios or []) if str(c).strip()],
        "aplicado": bool(aplicado),
    }

    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / CONSULTAS_DM_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
            f.flush()
        return True
    except Exception as e:  # noqa: BLE001 - nunca debe romper el turno ni la consulta
        logger.error("No se pudo registrar la consulta OOC en %s: %s", folder, e)
        return False


def load_consultas_dm(
    campaign_folder: Path | str,
    limit: Optional[int] = None,
) -> list[Dict[str, Any]]:
    """
    Carga las consultas OOC guardadas de la campaña, en orden cronológico.

    Tolera BOM, líneas vacías y líneas corruptas (un corte a mitad de escritura
    no invalida el resto del archivo). Con ``limit`` devuelve solo las últimas N.
    """
    folder = _carpeta_de_campana(campaign_folder)
    archivo = folder / CONSULTAS_DM_FILE
    consultas: list[Dict[str, Any]] = []
    if not archivo.exists():
        return consultas

    try:
        with open(archivo, "r", encoding="utf-8-sig") as f:
            for linea in f:
                texto = linea.strip()
                if not texto:
                    continue
                try:
                    registro = json.loads(texto)
                except Exception:
                    continue
                if isinstance(registro, dict):
                    registro.setdefault("v", 1)
                    consultas.append(registro)
    except Exception as e:  # noqa: BLE001
        logger.error("No se pudieron leer las consultas OOC de %s: %s", folder, e)
        return consultas

    if limit is not None:
        if limit <= 0:
            return []
        return consultas[-limit:]
    return consultas


def vaciar_consultas_dm(campaign_folder: Path | str) -> bool:
    """Vacía el archivo de consultas OOC (truncado atómico). ``True`` si no existía."""
    folder = _carpeta_de_campana(campaign_folder)
    archivo = folder / CONSULTAS_DM_FILE
    if not archivo.exists():
        return True

    temp_file = ""
    try:
        fd, temp_file = tempfile.mkstemp(
            dir=archivo.parent, prefix="tmp_save_", suffix=".tmp"
        )
        os.close(fd)
        os.replace(temp_file, archivo)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("No se pudo vaciar %s: %s", archivo, e)
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
        try:
            archivo.write_text("", encoding="utf-8")
            return True
        except Exception:
            return False


def append_jsonl(
    campaign_folder: Path | str,
    filename: str,
    record: Dict[str, Any],
    *,
    deduplicate_id: bool = True,
) -> bool:
    """Añade un registro JSON Lines de forma segura y tolerante a fallos."""
    folder = _carpeta_de_campana(campaign_folder)
    target = folder / filename

    if deduplicate_id and isinstance(record, dict) and record.get("id") and target.exists():
        rec_id = str(record["id"])
        try:
            with open(target, "r", encoding="utf-8-sig") as f:
                for line in f:
                    txt = line.strip()
                    if not txt:
                        continue
                    try:
                        prev = json.loads(txt)
                    except Exception:
                        continue
                    if isinstance(prev, dict) and str(prev.get("id", "")) == rec_id:
                        return True
        except OSError:
            pass

    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        return True
    except Exception as exc:  # noqa: BLE001 - nunca debe romper el turno
        logger.warning("No se pudo escribir registro JSONL en %s: %s", target, exc)
        return False


def load_jsonl(campaign_folder: Path | str, filename: str) -> list[Dict[str, Any]]:
    """Carga registros de un archivo JSON Lines tolerando líneas vacías o corruptas."""
    folder = _carpeta_de_campana(campaign_folder)
    target = folder / filename
    if not target.exists():
        return []

    registros: list[Dict[str, Any]] = []
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            for line in f:
                txt = line.strip()
                if not txt:
                    continue
                try:
                    obj = json.loads(txt)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    registros.append(obj)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo leer JSONL %s: %s", target, exc)
    return registros

