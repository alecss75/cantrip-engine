"""
save_migrations.py - Versionado del formato de partida y migración entre releases

`gamestate.json` es el activo más valioso del jugador: una actualización nunca
puede dejarlo a medio cargar. Este módulo centraliza la versión de esquema y la
escalera de migraciones.

Ubicación de la versión (ambas se aceptan por compatibilidad histórica):
- `gamestate["meta"]["schema_version"]` — lo que escribe `campaign_wizard`.
- `gamestate["schema_version"]`         — usado por tests y saves antiguos.

Reglas:
1. Idempotente: un save ya migrado no se vuelve a escribir (ni toca sus .bak).
2. Un backup `.v<N>.bak` con el original se crea antes de la primera escritura.
3. Un save anterior a `MIN_SOPORTADO` lanza `SaveTooOld`; nunca se carga a medias.
4. Las migraciones son funciones puras `dict -> dict`, sin red y sin efectos.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from src.storage.store import save_atomic

logger = logging.getLogger("cantrip.storage.save_migrations")

#: Versión de esquema que escribe esta release.
SCHEMA_VERSION = 3

#: Versión más antigua que esta release sabe migrar.
MIN_SOPORTADO = 1

#: Pendiente de v3: `mundo.npcs_presentes` pasó en v1.2.6 de lista de nombres a
#: entidades `{id, nombre, rol, ubicacion}` y la versión **no** sube todavía. El
#: motivo es el orden de carga: `migrar_esquema` corre dentro de
#: `store.load_json_safe`, antes de `npc_manager.configurar_campana()`, así que
#: una migración que resolviera IDs leería el índice de otra campaña. Ambas
#: formas se aceptan en ejecución (`normalizar_npcs_presentes`) y la resolución
#: con ID ocurre con la campaña ya configurada. Para la v3 hay que mover esa
#: resolución a después de `configurar_campana()` y migrar entonces, no antes.

#: Escalera de migraciones: versión_origen -> función que devuelve el dict migrado.
#:
#: Para añadir la v3:
#:   SCHEMA_VERSION = 3
#:   MIGRACIONES[2] = _v2_a_v3
#: La función debe ser pura, idempotente y tolerante a claves ausentes.
def _v2_a_v3(data: dict) -> dict:
    """Migración 2 → 3: añade ``meta.edition_creadora = 'community'`` sin tocar datos."""
    if not isinstance(data, dict):
        return data
    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        data["meta"] = meta
    meta.setdefault("edition_creadora", "community")
    return data


MIGRACIONES: dict[int, Callable[[dict], dict]] = {
    2: _v2_a_v3,
}


class SaveTooOld(Exception):
    """La partida es más antigua que la versión mínima que este motor sabe migrar."""

    def __init__(self, version_detectada: int, minima: int = MIN_SOPORTADO):
        self.version_detectada = version_detectada
        self.minima = minima
        super().__init__(
            f"La partida usa el formato v{version_detectada} y esta versión solo "
            f"puede migrar desde v{minima}."
        )


def version_de(data: Any) -> int:
    """Devuelve la versión de esquema de un gamestate, tolerando formato inválido."""
    if not isinstance(data, dict):
        return 1

    candidatos: list[Any] = []
    meta = data.get("meta")
    if isinstance(meta, dict):
        candidatos.append(meta.get("schema_version"))
    candidatos.append(data.get("schema_version"))

    for raw in candidatos:
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 1


def _fijar_version(data: dict, version: int) -> None:
    """Escribe la versión en el mismo sitio donde vivía (o en `meta` si no había)."""
    meta = data.get("meta")
    habia_en_meta = isinstance(meta, dict) and "schema_version" in meta
    habia_en_raiz = "schema_version" in data

    if habia_en_meta:
        meta["schema_version"] = version
    elif habia_en_raiz:
        data["schema_version"] = version
    else:
        if not isinstance(meta, dict):
            meta = {}
            data["meta"] = meta
        meta["schema_version"] = version

    # Si el save duplicaba la clave en la raíz, mantenerlas sincronizadas.
    if habia_en_raiz:
        data["schema_version"] = version


def migrar_esquema(data: dict, path: Path | str) -> dict:
    """
    Migra `data` hasta `SCHEMA_VERSION` dejando un backup del original.

    Devuelve el dict (migrado o intacto). Lanza `SaveTooOld` si la partida es
    demasiado antigua. Un fallo de escritura no rompe la carga: se migra en
    memoria y se registra el aviso.
    """
    if not isinstance(data, dict):
        return data

    actual = version_de(data)
    if actual >= SCHEMA_VERSION:
        return data
    if actual < MIN_SOPORTADO:
        raise SaveTooOld(actual)

    path = Path(path)

    backup = path.with_suffix(f".v{actual}.bak")
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            logger.warning("No se pudo crear el backup %s: %s", backup.name, exc)

    while actual < SCHEMA_VERSION:
        paso = MIGRACIONES.get(actual)
        if paso is None:
            logger.info(
                "Save en esquema v%s sin migración registrada: se conserva tal cual "
                "y se sella la versión actual.",
                actual,
            )
        else:
            data = paso(data)
        actual += 1

    _fijar_version(data, SCHEMA_VERSION)

    try:
        save_atomic(path, data, backups=0)
        logger.info("Partida migrada a schema_version %s (%s)", SCHEMA_VERSION, path.name)
    except OSError as exc:
        logger.warning(
            "Migración en memoria aplicada pero no persistida en %s: %s", path.name, exc
        )
    return data
