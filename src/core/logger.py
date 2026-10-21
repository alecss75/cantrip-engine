"""
logger.py - Logging Estructurado Centralizado

Configura un logger raíz único ("dnd_rpg") para toda la aplicación con:
1. Salida a consola (stderr) con niveles y timestamps.
2. Archivo rotativo en logs/dnd_rpg.log (10 MB x 5 backups de seguridad).
3. Configuración idempotente, invocable desde cualquier punto de entrada
   (main.py, launcher.py, GUI) sin duplicar handlers.

Nota de uso: los módulos internos (store, gateway, worker...) declaran su
logger con logging.getLogger("dnd_rpg.<modulo>") usando solo la stdlib, para
evitar dependencias circulares. Este módulo solo lo importan los entrypoints.

Los prints orientados a la interfaz de usuario (HUD, narrativa, menús) se
mantienen intactos; este logger canaliza diagnóstico, advertencias y errores.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.storage.fs_utils import get_user_data_dir
from src.core.credentials import SecretRedactionFilter

NOMBRE_LOGGER_RAIZ = "dnd_rpg"

# Directorio de datos del usuario (raíz en código fuente o carpeta escribible en binario)
_PROJECT_ROOT = get_user_data_dir()
_LOGS_DIR = _PROJECT_ROOT / "logs"

_configurado = False


def setup_logging(
    log_level: str | None = None,
    log_file: Path | str | None = None,
) -> logging.Logger:
    """
    Configura el logger raíz de la aplicación de forma idempotente.

    Args:
        log_level: Nivel de logging (DEBUG/INFO/WARNING/ERROR). Por defecto,
                   variable de entorno LOG_LEVEL o INFO.
        log_file: Ruta del archivo de log. Por defecto logs/dnd_rpg.log.

    Returns:
        El logger raíz "dnd_rpg" configurado.
    """
    global _configurado
    logger = logging.getLogger(NOMBRE_LOGGER_RAIZ)
    if _configurado:
        return logger

    nivel = str(log_level or os.getenv("LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, nivel, logging.INFO))
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redaction_filter = SecretRedactionFilter()
    if not any(isinstance(f, SecretRedactionFilter) for f in logger.filters):
        logger.addFilter(redaction_filter)

    # Handler de consola (stderr para no interferir con la salida de juego)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        consola = logging.StreamHandler(sys.stderr)
        consola.setFormatter(formatter)
        consola.addFilter(redaction_filter)
        logger.addHandler(consola)

    # Handler de archivo rotativo
    destino = Path(log_file) if log_file else _LOGS_DIR / "dnd_rpg.log"
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            archivo = RotatingFileHandler(
                destino,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            archivo.setFormatter(formatter)
            archivo.addFilter(redaction_filter)
            logger.addHandler(archivo)
        except OSError:
            # Entornos de solo lectura o sin permisos: continuar solo con consola
            logger.warning("No se pudo crear el archivo de log en %s; solo consola.", destino)

    _configurado = True
    return logger


def get_logger(nombre: str | None = None) -> logging.Logger:
    """
    Devuelve un logger hijo del logger raíz de la aplicación.

    Args:
        nombre: Sufijo del módulo (ej. "store" -> "dnd_rpg.store").
    """
    if not _configurado:
        setup_logging()
    if not nombre:
        return logging.getLogger(NOMBRE_LOGGER_RAIZ)
    return logging.getLogger(f"{NOMBRE_LOGGER_RAIZ}.{nombre}")
