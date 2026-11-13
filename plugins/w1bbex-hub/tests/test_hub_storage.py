"""Tests for portable Hub paths, atomic storage persistence, and deferred deletions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src import runtime, storage


def test_resolve_paths_independent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cardinal_root = tmp_path / "Cardinal"
    plugin_file = cardinal_root / "plugins" / "W1bbexHub.py"
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(other_cwd)

    resolved = runtime.resolve_paths(anchor_file=plugin_file)
    assert Path(resolved.cardinal_root) == cardinal_root.resolve()
    assert Path(resolved.plugins_dir) == (cardinal_root / "plugins").resolve()
    assert Path(resolved.data_file) == (cardinal_root / "storage" / "w1bbex" / "Hub" / "config.json").resolve()
    assert Path(resolved.log_file) == (cardinal_root / "storage" / "w1bbex" / "Hub" / "logs" / "w1bbex_hub.log").resolve()


def test_load_data_initializes_and_backs_up_corrupt_json(isolated_hub: Path) -> None:
    data = storage.load_data()
    assert data == {"keys": {}}
    assert Path(runtime.PATHS.data_file).is_file()

    Path(runtime.PATHS.data_file).write_text("{corrupt json", encoding="utf-8")
    recovered = storage.load_data()
    assert recovered == {"keys": {}}
    backups = list(Path(runtime.PATHS.hub_folder).glob("config.json.corrupt.*"))
    assert len(backups) == 1


def test_save_data_atomic_failure_preserves_existing_file(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = {"keys": {"autooffline": "AO-1111"}}
    storage.save_data(original)

    def fail_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        storage.save_data({"keys": {"autooffline": "OVERWRITTEN"}})

    assert json.loads(Path(runtime.PATHS.data_file).read_text(encoding="utf-8")) == original
    leftovers = list(Path(runtime.PATHS.hub_folder).glob(".tmp_*"))
    assert leftovers == []


def test_pending_deletion_queues_locked_folder_and_cleans_later(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked_dir = isolated_hub / "plugins" / "w1bbex" / "autooffline_v1.0"
    locked_dir.mkdir(parents=True, exist_ok=True)
    (locked_dir / "AutoOffline.pyd").write_bytes(b"binary")

    real_rmtree = storage.shutil.rmtree

    def fail_first_rmtree(path: str) -> None:
        raise PermissionError("file in use")

    monkeypatch.setattr(storage.shutil, "rmtree", fail_first_rmtree)
    assert storage.queue_pending_deletion(str(locked_dir)) is True
    assert Path(runtime.PATHS.pending_delete_file).is_file()
    assert str(locked_dir) in json.loads(Path(runtime.PATHS.pending_delete_file).read_text(encoding="utf-8"))

    monkeypatch.setattr(storage.shutil, "rmtree", real_rmtree)
    storage.process_pending_deletions()
    assert not locked_dir.exists()
    assert not Path(runtime.PATHS.pending_delete_file).exists()


def test_pending_restart_persistence_and_ttl_expiry(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(storage.time, "time", lambda: now[0])

    storage.save_pending_restart({"action": "install", "plugin_id": "autosmm", "chat_id": 555})
    assert storage.pop_pending_restart(300.0)["plugin_id"] == "autosmm"
    assert storage.pop_pending_restart(300.0) is None

    storage.save_pending_restart({"action": "hub", "chat_id": 555})
    now[0] = 1500.0
    assert storage.pop_pending_restart(300.0) is None
    assert "pending_restart" not in storage.load_data()


def test_corrupt_json_creates_single_backup_and_rejects_outside_deletions(isolated_hub: Path) -> None:
    data_path = Path(runtime.PATHS.data_file)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text("{corrupt json", encoding="utf-8")
    assert storage.load_data() == {"keys": {}}
    assert storage.load_data() == {"keys": {}}
    backups = list(Path(runtime.PATHS.hub_folder).glob("config.json.corrupt.*"))
    assert len(backups) == 1

    outside_dir = isolated_hub / "storage" / "w1bbex" / "ImportantData"
    outside_dir.mkdir(parents=True, exist_ok=True)
    secret_file = outside_dir / "keep.txt"
    secret_file.write_text("precious", encoding="utf-8")

    assert storage.queue_pending_deletion(str(outside_dir)) is False
    assert secret_file.is_file()

    Path(runtime.PATHS.pending_delete_file).write_text(json.dumps([str(outside_dir)]), encoding="utf-8")
    storage.process_pending_deletions()
    assert secret_file.is_file()
