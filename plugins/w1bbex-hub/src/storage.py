"""Atomic JSON/binary persistence and deferred plugin directory deletion."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import Any, Callable, Optional

from .runtime import DATA_LOCK, DELETE_LOCK, PATHS, logger


def atomic_write_bytes(target_path: str, content: bytes) -> None:
    """Write bytes atomically via same-directory temp file, fsync, and os.replace."""
    target_abs = os.path.abspath(target_path)
    parent = os.path.dirname(target_abs)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".part", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_abs)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("Failed removing temp file %s", tmp_path, exc_info=True)
        raise


def atomic_write_json(target_path: str, payload: Any) -> None:
    """Serialize payload as UTF-8 JSON and write atomically."""
    raw = json.dumps(payload, indent=4, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(target_path, raw)


def _is_within_managed_plugins_dir(target_path: Optional[str]) -> bool:
    """Return True only when target_path points to a subdirectory inside plugins/w1bbex/."""
    if not target_path or not isinstance(target_path, str):
        return False
    w1bbex_root = os.path.realpath(os.path.join(PATHS.plugins_dir, "w1bbex"))
    target_real = os.path.realpath(target_path)
    try:
        common = os.path.commonpath([w1bbex_root, target_real])
    except ValueError:
        return False
    return common == w1bbex_root and target_real != w1bbex_root


def process_pending_deletions() -> None:
    """Retry removing plugin directories deferred while binary files were locked."""
    pending_file = PATHS.pending_delete_file
    with DELETE_LOCK:
        if not os.path.exists(pending_file):
            return
        try:
            with open(pending_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            pending = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            logger.exception("Failed reading pending deletions file: %s", pending_file)
            return

        still_pending: list[str] = []
        for path in pending:
            if isinstance(path, str) and path and os.path.exists(path):
                if not _is_within_managed_plugins_dir(path):
                    logger.warning("Refusing deferred deletion outside plugins/w1bbex/: %s", path)
                    continue
                try:
                    shutil.rmtree(path)
                    logger.info("Removed deferred plugin folder: %s", path)
                except OSError as exc:
                    logger.warning("Deferred folder still locked (%s): %s", path, exc)
                    still_pending.append(path)

        try:
            if still_pending:
                atomic_write_json(pending_file, still_pending)
            elif os.path.exists(pending_file):
                os.remove(pending_file)
        except OSError:
            logger.exception("Failed updating pending deletions file")


def queue_pending_deletion(target_dir: Optional[str]) -> bool:
    """Delete target_dir immediately or queue for next startup if locked. Returns True if deferred."""
    if not target_dir or not os.path.exists(target_dir):
        return False
    if not _is_within_managed_plugins_dir(target_dir):
        logger.warning("Refusing deletion outside plugins/w1bbex/: %s", target_dir)
        return False

    pending_file = PATHS.pending_delete_file
    with DELETE_LOCK:
        try:
            shutil.rmtree(target_dir)
            return False
        except OSError as exc:
            logger.warning("Deferring deletion of locked folder %s: %s", target_dir, exc)

        pending: list[str] = []
        if os.path.exists(pending_file):
            try:
                with open(pending_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    pending = [p for p in loaded if isinstance(p, str)]
            except (OSError, ValueError):
                logger.warning("Rebuilding corrupt pending_delete.json", exc_info=True)
        if target_dir not in pending:
            pending.append(target_dir)
        atomic_write_json(pending_file, pending)
        return True


def load_data() -> dict[str, Any]:
    """Load Hub config from disk, moving corrupt JSON aside once instead of re-copying every read."""
    data_file = PATHS.data_file
    with DATA_LOCK:
        if not os.path.exists(data_file):
            default_data: dict[str, Any] = {"keys": {}}
            try:
                atomic_write_json(data_file, default_data)
            except OSError:
                logger.warning("Could not initialize config at %s", data_file, exc_info=True)
            return default_data

        try:
            with open(data_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
            logger.error("Hub config at %s has non-dict root; backing up", data_file)
        except (OSError, ValueError):
            logger.exception("Failed reading Hub config at %s", data_file)

        corrupt_backup = f"{data_file}.corrupt.{int(time.time())}"
        try:
            os.replace(data_file, corrupt_backup)
        except OSError:
            logger.warning("Failed to isolate corrupt config to %s", corrupt_backup, exc_info=True)
        default_data = {"keys": {}}
        try:
            atomic_write_json(data_file, default_data)
        except OSError:
            logger.warning("Could not reset corrupt config at %s", data_file, exc_info=True)
        return default_data


def save_data(data: dict[str, Any]) -> None:
    """Persist Hub config atomically, propagating disk errors to callers."""
    with DATA_LOCK:
        atomic_write_json(PATHS.data_file, data)


def update_data(mutator: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
    """Perform an atomic read-modify-write update under DATA_LOCK."""
    with DATA_LOCK:
        data = load_data()
        result = mutator(data)
        save_data(data)
        return data, result


def save_pending_restart(payload: dict[str, Any]) -> None:
    """Persist pending post-restart notification metadata under DATA_LOCK."""
    record = dict(payload)
    record.setdefault("created_at", time.time())
    update_data(lambda d: d.__setitem__("pending_restart", record))


def _validate_pending_restart(raw: Any, max_age_seconds: float) -> Optional[dict[str, Any]]:
    """Return shallow copy when pending_restart is a valid dict within max_age_seconds."""
    if not isinstance(raw, dict):
        return None
    try:
        created_at = float(raw.get("created_at", 0.0))
    except (TypeError, ValueError):
        return None
    if max_age_seconds > 0 and (time.time() - created_at) > max_age_seconds:
        return None
    return dict(raw)


def clear_pending_restart() -> None:
    """Remove pending_restart metadata from disk under DATA_LOCK."""
    update_data(lambda d: d.pop("pending_restart", None))


def peek_pending_restart(max_age_seconds: float) -> Optional[dict[str, Any]]:
    """Inspect pending_restart non-destructively, pruning only corrupt or expired markers."""
    with DATA_LOCK:
        data = load_data()
        if "pending_restart" not in data:
            return None
        valid = _validate_pending_restart(data.get("pending_restart"), max_age_seconds)
        if valid is None:
            data.pop("pending_restart", None)
            save_data(data)
            return None
        return valid


def pop_pending_restart(max_age_seconds: float) -> Optional[dict[str, Any]]:
    """Atomically pop pending_restart if present and within max_age_seconds."""
    def _pop(data: dict[str, Any]) -> Optional[dict[str, Any]]:
        raw = data.pop("pending_restart", None)
        return _validate_pending_restart(raw, max_age_seconds)

    _, popped = update_data(_pop)
    return popped
