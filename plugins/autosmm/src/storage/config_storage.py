"""AutoSMM configuration and shared atomic JSON file access."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Callable, Dict

from .. import runtime as rt
from .defaults import _ensure_config_defaults, create_default_config

logger = logging.getLogger("FPC.autosmm")

CONFIG_LOCK = threading.RLock()

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[str, threading.RLock] = {}


def _get_path_lock(path: str) -> threading.RLock:
    """Return a per-file reentrant lock so concurrent writers never collide on os.replace."""
    key = os.path.normcase(os.path.abspath(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _make_temp_path(path: str) -> str:
    """Build a collision-free temporary file path in the target directory."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"


def _replace_with_retry(temp_path: str, abs_path: str, attempts: int = 5) -> None:
    """Retry os.replace briefly on transient Windows sharing/permission locks."""
    for attempt in range(attempts):
        try:
            os.replace(temp_path, abs_path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.01 * (attempt + 1))


def _write_bytes_atomic(path: str, raw_bytes: bytes) -> None:
    """Atomically write raw bytes using a unique temp file, flush/fsync, and os.replace."""
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = _make_temp_path(abs_path)
    with _get_path_lock(abs_path):
        try:
            with open(temp_path, "wb") as fh:
                fh.write(raw_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(temp_path, abs_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError as exc:
                    logger.debug(f"Не удалось удалить временный файл {temp_path}: {exc}")


def _write_json_atomic(path: str, data: Any) -> None:
    """Atomically persist JSON using UTF-8 serialization and atomic byte replacement."""
    encoded = json.dumps(data, indent=4, ensure_ascii=False).encode("utf-8")
    _write_bytes_atomic(path, encoded)


def _safe_load_json(path: str, default: Any) -> Any:
    """Load JSON safely, archiving corrupt files instead of crashing."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error(f"Ошибка при чтении {os.path.basename(path)}: {exc}")
        backup_path = f"{path}.bak.{int(time.time())}"
        try:
            shutil.copy2(path, backup_path)
            logger.info(f"Создана резервная копия поврежденного файла: {backup_path}")
        except OSError as bak_exc:
            logger.error(f"Не удалось создать резервную копию {os.path.basename(path)}: {bak_exc}")
        return default
    except OSError as exc:
        logger.error(f"Ошибка при чтении {os.path.basename(path)}: {exc}")
        return default


def _safe_write_json(path: str, data: Any) -> bool:
    """Write JSON atomically and return explicit success status."""
    try:
        _write_json_atomic(path, data)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.exception(f"Ошибка при сохранении {os.path.basename(path)}: {exc}")
        return False


def _sync_runtime_config_refs(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        return
    with CONFIG_LOCK:
        rt.config.clear()
        rt.config.update(copy.deepcopy(cfg))
        lot_map = cfg.get("lot_mapping")
        rt.lot_mapping.clear()
        if isinstance(lot_map, dict):
            rt.lot_mapping.update(copy.deepcopy(lot_map))


def load_config() -> Dict[str, Any]:
    """Load auto_lots.json with automatic .bak recovery and default creation."""
    logger.debug("Загрузка конфигурации (файл настроек)...")
    with CONFIG_LOCK:
        raw_data = None
        if os.path.exists(rt.CONFIG_PATH):
            try:
                with open(rt.CONFIG_PATH, "r", encoding="utf-8") as fh:
                    file_content = fh.read()
                if file_content.strip():
                    raw_data = json.loads(file_content)
                else:
                    logger.error(f"Файл конфигурации {rt.CONFIG_PATH} пуст.")
            except json.JSONDecodeError as exc:
                logger.error(f"Ошибка при чтении JSON {rt.CONFIG_PATH}: {exc}")
            except OSError as exc:
                logger.error(f"Ошибка при открытии {rt.CONFIG_PATH}: {exc}")
        else:
            logger.debug(f"Файл конфигурации {rt.CONFIG_PATH} не найден. Проверяем резервную копию.")

        if (not isinstance(raw_data, dict) or not raw_data) and os.path.exists(rt.CONFIG_BAK_PATH):
            try:
                if os.path.getsize(rt.CONFIG_BAK_PATH) > 0:
                    logger.warning(f"Попытка восстановить конфиг из резервной копии {rt.CONFIG_BAK_PATH}...")
                    with open(rt.CONFIG_BAK_PATH, "r", encoding="utf-8") as fh:
                        bak_content = fh.read().strip()
                    if bak_content:
                        bak_data = json.loads(bak_content)
                        if isinstance(bak_data, dict) and bak_data:
                            logger.info(f"Конфигурация успешно восстановлена из {rt.CONFIG_BAK_PATH}!")
                            raw_data = bak_data
                            save_config(raw_data)
            except (OSError, ValueError) as bak_exc:
                logger.error(f"Ошибка восстановления из {rt.CONFIG_BAK_PATH}: {bak_exc}")

        if not isinstance(raw_data, dict) or not raw_data:
            corrupt_path = f"{rt.CONFIG_PATH}.corrupt_{int(time.time())}"
            if os.path.exists(rt.CONFIG_PATH):
                try:
                    shutil.copy2(rt.CONFIG_PATH, corrupt_path)
                    logger.warning(f"Поврежденный конфиг скопирован в {corrupt_path}")
                except OSError as copy_exc:
                    logger.warning(f"Не удалось скопировать поврежденный конфиг в {corrupt_path}: {copy_exc}")
            logger.error("Не удалось прочитать конфиг. Создаем конфигурацию по умолчанию.")
            cfg = create_default_config()
            save_config(cfg)
            return cfg

        if _ensure_config_defaults(raw_data):
            save_config(raw_data)
        _sync_runtime_config_refs(raw_data)
        return raw_data


def save_config(cfg: Dict[str, Any]) -> None:
    """Persist auto_lots.json atomically and sync auto_lots.json.bak."""
    logger.debug("Сохранение конфигурации (файл настроек)...")
    with CONFIG_LOCK:
        try:
            _write_json_atomic(rt.CONFIG_PATH, cfg)
            _sync_runtime_config_refs(cfg)
            logger.debug("Конфигурация сохранена атомарно.")
        except Exception as exc:
            logger.exception(f"Ошибка при атомарном сохранении конфигурации: {exc}")
            raise
        try:
            _write_json_atomic(rt.CONFIG_BAK_PATH, cfg)
        except (OSError, ValueError, TypeError) as bak_err:
            logger.warning(f"Не удалось создать backup конфига: {bak_err}")


def update_config(mutator: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    """Read-modify-write auto_lots.json atomically under CONFIG_LOCK."""
    with CONFIG_LOCK:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return cfg


def reindex_lots(cfg: Dict[str, Any]) -> None:
    """Renumber lot keys sequentially as lot_1..lot_N and save configuration."""
    with CONFIG_LOCK:
        lot_map = cfg.get("lot_mapping", {})

        def _lot_key_sort(item: Any) -> Any:
            key = str(item[0])
            match = re.match(r"^lot_(\d+)$", key)
            return (0, int(match.group(1))) if match else (1, key)

        sorted_lots = sorted(lot_map.items(), key=_lot_key_sort)
        cfg["lot_mapping"] = {
            f"lot_{idx}": lot_data
            for idx, (_lot_key, lot_data) in enumerate(sorted_lots, start=1)
        }
        save_config(cfg)
        logger.info("Лоты были переиндексированы.")
