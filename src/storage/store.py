"""
store.py - Capa de Persistencia Atómica y Transaccional

Garantiza integridad de datos ante caídas de proceso, cortes de luz o
bloqueos temporales de sincronización (OneDrive/Dropbox):
1. Escritura atómica vía archivo temporal en el mismo volumen + os.replace.
2. Rotación automática de respaldos (.bak1, .bak2, .bak3).
3. Recuperación automática desde respaldo ante corrupción de archivo.
4. Historial transaccional en JSON Lines (history.jsonl).
5. Admite indistintamente rutas de archivo JSON o carpetas de campaña.
"""

import json
import logging
import os
import shutil
import tempfile
import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("cantrip.store")


def save_atomic(target_path: Path | str, data: dict[str, Any], backups: int = 3) -> bool:
    """
    Guarda un diccionario en formato JSON de forma atómica.
    Acepta tanto una ruta directa al archivo .json como a la carpeta de la campaña.
    
    1. Rota copias de seguridad existentes (.bak1, .bak2...).
    2. Escribe en un archivo temporal en la misma carpeta.
    3. Realiza un flush + fsync a disco físico.
    4. Reemplaza el archivo destino atómicamente con os.replace.
    """
    path = Path(target_path).resolve()
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
        raise OSError(f"Error crítico en guardado atómico de {path}: {e}")


def load_json_safe(target_path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Carga un archivo JSON de forma segura.
    Si el archivo está dañado o truncado, intenta recuperarse desde el backup más reciente.
    Si no hay backup viable, devuelve el valor default o lanza un error.
    """
    path = Path(target_path).resolve()
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
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Archivo corrupto detectado en %s: %s", path, e)
        # Intentar restaurar desde backup
        for i in range(1, 4):
            bak_path = path.with_suffix(f".json.bak{i}")
            if bak_path.exists():
                try:
                    with open(bak_path, encoding='utf-8-sig') as bf:
                        recovered = json.load(bf)
                    logger.info("Recuperado exitosamente desde %s", bak_path.name)
                    # Restaurar archivo principal con el backup sano
                    save_atomic(path, recovered, backups=0)
                    return recovered
                except Exception:
                    continue

        if default is not None:
            logger.warning("No se pudo recuperar ningún backup de %s. Usando estado por defecto.", path)
            return default
        raise OSError(f"Archivo {path} y todos sus respaldos están corruptos.")


# Alias de compatibilidad
load_with_recovery = load_json_safe


def append_history_log(
    campaign_folder: Path | str,
    entry_or_action: Any,
    narrativa_dm: str | None = None,
    metadatos: dict[str, Any] | None = None
) -> bool:
    """
    Añade una entrada al historial de la campaña en formato JSON Lines (history.jsonl).
    Garantiza que el log de turnos sobreviva de forma incremental e inmutable.
    Acepta tanto un diccionario directo como argumentos separados (accion, narrativa, metadatos).
    """
    folder = Path(campaign_folder).resolve()
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


def load_history_log(campaign_folder: Path | str) -> list[dict[str, Any]]:
    """Carga todas las entradas del historial de la campaña."""
    folder = Path(campaign_folder).resolve()
    if folder.is_file() or folder.suffix:
        folder = folder.parent
    log_file = folder / "history.jsonl"
    if not log_file.exists():
        return []

    entries = []
    with open(log_file, encoding='utf-8-sig') as f:
        for line in f:
            line_str = line.strip()
            if line_str:
                try:
                    entries.append(json.loads(line_str))
                except Exception:
                    continue
    return entries
