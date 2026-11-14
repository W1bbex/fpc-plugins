"""Unit tests for Hub version checks, cache locking, plugin downloads, loader, and self-updates."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import REPO_ROOT, make_fake_cardinal
from src import core, hub_api, installer, runtime, storage


def test_is_newer_version_handles_downgrades_and_prefixes() -> None:
    assert core.is_newer_version("0.2.0", "0.1.0") is True
    assert core.is_newer_version("v1.0.1", "v1.0.0") is True
    assert core.is_newer_version("0.1.0", "0.2.0") is False
    assert core.is_newer_version("v1.0", "1.0") is False
    assert core.is_newer_version("1.0-beta2", "1.0-beta1") is True


def test_identity_extractors_never_emit_none_string() -> None:
    cardinal = make_fake_cardinal(fp_id=None, fp_username=None)
    fp = hub_api.get_funpay_identity(cardinal)
    assert fp["id"] == ""
    assert fp["username"] == "-"

    cardinal_partial = SimpleNamespace(account=SimpleNamespace(id=None, user_id=None, username=None))
    fp2 = hub_api.get_funpay_identity(cardinal_partial)
    assert fp2["id"] == ""
    assert fp2["username"] == "-"

    tg = hub_api.get_telegram_identity(SimpleNamespace(id=None, username=None))
    assert tg["id"] == ""
    assert tg["username"] == ""

    tg2 = hub_api.get_telegram_identity(SimpleNamespace(id=777, username="@w1bbex"))
    assert tg2["id"] == "777"
    assert tg2["username"] == "w1bbex"


def test_get_plugins_releases_cache_lock_before_http_and_returns_copies(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_held_during_http = []

    def fake_get(url: str, timeout: int, proxies: dict):
        acquired = runtime.CACHE_LOCK.acquire(blocking=False)
        lock_held_during_http.append(not acquired)
        if acquired:
            runtime.CACHE_LOCK.release()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "success": True,
            "plugins": [{"id": "autooffline", "name": "AutoOffline", "prefix": "AO"}],
        }
        return resp

    monkeypatch.setattr(hub_api.requests, "get", fake_get)
    items = hub_api.get_plugins()
    assert lock_held_during_http == [False]
    assert len(items) == 1
    items[0]["name"] = "MUTATED"
    assert hub_api.get_plugins()[0]["name"] == "AutoOffline"


def test_download_plugin_preserves_so_extension_and_generates_working_loader(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.PLUGINS_CACHE["items"] = [
        {
            "id": "autooffline",
            "name": 'AutoOffline "Pro"',
            "prefix": "AO",
            "latest_version": "v1.2.0",
            "description": "Plugin 'desc'\nline2",
        }
    ]
    runtime.PLUGINS_CACHE["fetched_at"] = 9e12

    def fake_post(url: str, json: dict, timeout: int, proxies: dict):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {
            "Content-Type": "application/octet-stream",
            "X-Filename": "../../../../autooffline.cpython-312-x86_64-linux-gnu.so",
        }
        resp.content = b"\x7fELF_FAKE_SO_BINARY"
        return resp

    monkeypatch.setattr(installer.requests, "post", fake_post)
    cardinal = make_fake_cardinal()
    ok, msg = installer.download_plugin(cardinal, "autooffline", "AO-1111", SimpleNamespace(id=42, username="buyer"))

    assert ok is True
    assert "v1.2.0" in msg

    saved = isolated_hub / "plugins" / "w1bbex" / "autooffline_v1.2.0" / "autooffline.cpython-312-x86_64-linux-gnu.so"
    assert saved.is_file()
    assert not (isolated_hub / "plugins" / "w1bbex" / "autooffline_v1.2.0" / "autooffline.py").exists()

    loader_file = isolated_hub / "plugins" / "AutoOffline.py"
    assert loader_file.is_file()
    loader_src = loader_file.read_text(encoding="utf-8")
    assert loader_src.startswith(runtime.LOADER_HEADER)
    compile(loader_src, str(loader_file), "exec")

    data = storage.load_data()
    assert data["installed"]["autooffline"]["version"] == "v1.2.0"


def test_generated_loader_executes_installed_py_plugin(isolated_hub: Path) -> None:
    plugin_dir = isolated_hub / "plugins" / "w1bbex" / "SamplePlugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "sample.py").write_text(
        'UUID = "1111-2222"\nCREDITS = "@w1bbex"\nSETTINGS_PAGE = True\nPING = "pong"\n',
        encoding="utf-8",
    )

    loader_path = installer.generate_loader(
        plugin_name="sample",
        plugin_label="SamplePlugin",
        folder_name="SamplePlugin",
        api_name='Sample "Quoted"',
        api_version="v1.0",
        api_desc="Line 1\nLine 2",
    )
    spec = importlib.util.spec_from_file_location("test_loaded_sample", loader_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PING == "pong"
    assert mod.UUID == "1111-2222"
    assert mod.CREDITS == "@w1bbex"
    assert mod.NAME == 'Sample "Quoted"'
    assert mod.SETTINGS_PAGE is True


def test_download_plugin_handles_locked_binary_reinstall_cleanly(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_post(url: str, json: dict, timeout: int, proxies: dict):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/octet-stream", "X-Filename": "mod.pyd"}
        resp.content = b"MZ"
        return resp

    monkeypatch.setattr(installer.requests, "post", fake_post)
    monkeypatch.setattr(installer, "atomic_write_bytes", MagicMock(side_effect=PermissionError("locked")))
    ok, msg = installer.download_plugin(make_fake_cardinal(), "autooffline", "AO-KEY", SimpleNamespace(id=1, username="u"))
    assert ok is False
    assert "/restart" in msg


def test_validate_hub_source_accepts_built_bundle_containing_word_success(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_bundle_bytes = (REPO_ROOT / "W1bbexHub.py").read_bytes()
    assert b"success" in real_bundle_bytes

    ok, err = installer.validate_hub_source(real_bundle_bytes, "text/plain; charset=utf-8")
    assert ok is True, err

    target_hub = isolated_hub / "plugins" / "W1bbexHub.py"

    def fake_get(url: str, timeout: int, proxies: dict):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/plain; charset=utf-8"}
        resp.content = real_bundle_bytes
        return resp

    monkeypatch.setattr(installer.requests, "get", fake_get)
    upd_ok, upd_msg = installer.download_and_update_hub(str(target_hub))
    assert upd_ok is True
    assert target_hub.read_bytes() == real_bundle_bytes


def test_download_and_update_hub_rejects_json_and_broken_python(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_hub = isolated_hub / "plugins" / "W1bbexHub.py"
    target_hub.write_text("# original intact hub", encoding="utf-8")

    for headers, body in [
        ({"Content-Type": "application/json"}, b'{"success": false, "error": "missing"}'),
        ({"Content-Type": "text/html"}, b"<html>502 Bad Gateway</html>"),
        ({"Content-Type": "text/plain"}, b"def broken(:\n"),
        ({"Content-Type": "text/plain"}, b"X = 1\n"),
    ]:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = headers
        resp.content = body
        monkeypatch.setattr(installer.requests, "get", lambda *a, r=resp, **kw: r)
        ok, _ = installer.download_and_update_hub(str(target_hub))
        assert ok is False
        assert target_hub.read_text(encoding="utf-8") == "# original intact hub"


def test_download_free_py_plugin_archives_legacy_w1bbex_duplicates_only(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_uuid = "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8"
    plugins_dir = isolated_hub / "plugins"

    # Legacy duplicate with matching UUID and @w1bbex creator
    legacy_old = plugins_dir / "AutoSMM_0.1.py"
    legacy_old.write_text(
        f'UUID = "{target_uuid}"\nCREDITS = "@w1bbex"\nVERSION = "0.0.9"\n',
        encoding="utf-8",
    )
    # Mixed-case creator duplicate also matching UUID
    legacy_case = plugins_dir / "AutoSMM_legacy.py"
    legacy_case.write_text(
        f'UUID = "{target_uuid.upper()}"\nCREDITS = "@W1BBEX"\n',
        encoding="utf-8",
    )
    # Must NOT archive: different creator or different UUID or invalid syntax
    other_author = plugins_dir / "ForkSameUUID.py"
    other_author.write_text(
        f'UUID = "{target_uuid}"\nCREDITS = "@other_dev"\n',
        encoding="utf-8",
    )
    other_plugin = plugins_dir / "AutoOffline.py"
    other_plugin.write_text(
        'UUID = "99999999-1111-2222-3333-444444444444"\nCREDITS = "@w1bbex"\n',
        encoding="utf-8",
    )
    broken_file = plugins_dir / "BrokenSyntax.py"
    broken_file.write_text("def oops(:\n", encoding="utf-8")

    # Plugin user data storage must remain untouched
    lots_file = isolated_hub / "storage" / "w1bbex" / "AutoSMM" / "auto_lots.json"
    lots_file.parent.mkdir(parents=True, exist_ok=True)
    lots_file.write_text('{"lots": {"demo": 1}}', encoding="utf-8")

    runtime.PLUGINS_CACHE["items"] = [
        {
            "id": "autosmm",
            "name": "AutoSMM",
            "prefix": "AS:",
            "latest_version": "1.0",
            "description": "AutoSMM description",
            "is_free": True,
            "uuid": target_uuid,
            "credits": "@w1bbex",
        }
    ]
    runtime.PLUGINS_CACHE["fetched_at"] = 9e12

    new_source = (
        f'UUID = "{target_uuid}"\n'
        'CREDITS = "@w1bbex"\n'
        'SETTINGS_PAGE = False\n'
        'BIND_TO_DELETE = []\n'
        'MARKER = "v1.0"\n'
    ).encode("utf-8")

    def fake_post(url: str, json: dict, timeout: int, proxies: dict):
        assert json["license_key"] == ""
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/octet-stream", "X-Filename": "AutoSMM_1.0.py"}
        resp.content = new_source
        return resp

    monkeypatch.setattr(installer.requests, "post", fake_post)
    ok, msg = installer.download_plugin(make_fake_cardinal(), "autosmm", None, SimpleNamespace(id=10, username="u"))
    assert ok is True
    assert "AutoSMM_0.1.py" in msg

    canonical_py = plugins_dir / "w1bbex" / "AutoSMM" / "AutoSMM_1.0.py"
    assert canonical_py.is_file()
    assert canonical_py.read_bytes() == new_source

    stub = plugins_dir / "AutoSMM.py"
    assert stub.is_file()
    assert stub.read_text(encoding="utf-8").startswith(runtime.LOADER_HEADER)

    # Verify stub executes cleanly
    spec = importlib.util.spec_from_file_location("test_autosmm_stub", stub)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    assert loaded.MARKER == "v1.0"
    assert loaded.UUID == target_uuid
    assert loaded.CREDITS == "@w1bbex"

    # Legacy files archived, unrelated files untouched, storage untouched
    assert not legacy_old.exists()
    assert not legacy_case.exists()
    archived_dir = plugins_dir / "w1bbex" / "archive"
    archived_names = [p.name for p in archived_dir.glob("*.py")]
    assert any(name.startswith("AutoSMM_0.1_") for name in archived_names)
    assert any(name.startswith("AutoSMM_legacy_") for name in archived_names)

    assert other_author.is_file()
    assert other_plugin.is_file()
    assert broken_file.is_file()
    assert lots_file.read_text(encoding="utf-8") == '{"lots": {"demo": 1}}'

    # Reinstall newer .py filename removes stale .py inside plugins/w1bbex/AutoSMM and does not archive Hub stub
    runtime.PLUGINS_CACHE["items"][0]["latest_version"] = "1.1"
    newer_source = new_source.replace(b'v1.0', b'v1.1')

    def fake_post_v2(url: str, json: dict, timeout: int, proxies: dict):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/octet-stream", "X-Filename": "AutoSMM_1.1.py"}
        resp.content = newer_source
        return resp

    monkeypatch.setattr(installer.requests, "post", fake_post_v2)
    ok2, msg2 = installer.download_plugin(make_fake_cardinal(), "autosmm", None, SimpleNamespace(id=10, username="u"))
    assert ok2 is True
    assert "archive" not in msg2
    assert not canonical_py.exists()
    assert (plugins_dir / "w1bbex" / "AutoSMM" / "AutoSMM_1.1.py").is_file()


def test_inspect_plugin_state_tracks_ram_disk_and_manual_lifecycle(isolated_hub: Path) -> None:
    target_uuid = "a6ccb3f1-3e7b-4984-9e4c-01d3ea7e0ad8"
    plugin_info = {
        "id": "autosmm",
        "name": "AutoSMM",
        "latest_version": "1.0.1",
        "is_free": True,
        "uuid": target_uuid,
        "credits": "@w1bbex",
    }
    cardinal = make_fake_cardinal()
    cardinal.plugins = {}

    st_none = core.inspect_plugin_state(cardinal, plugin_info, {}, {})
    assert st_none["status_code"] == "not_installed"
    assert st_none["needs_restart"] is False

    # Manual file in plugins/ detected via AST even before loading
    manual_file = isolated_hub / "plugins" / "AutoSMM_old.py"
    manual_file.write_text(f'UUID = "{target_uuid}"\nCREDITS = "@w1bbex"\nVERSION = "0.9"\n', encoding="utf-8")
    st_manual = core.inspect_plugin_state(cardinal, plugin_info, {}, {})
    assert st_manual["status_code"] == "manual"
    assert st_manual["manual_files"][0]["filename"] == "AutoSMM_old.py"
    manual_file.unlink()

    # Installed on disk, not yet loaded in RAM -> restart_install
    stub = isolated_hub / "plugins" / "AutoSMM.py"
    stub.write_text(f"{runtime.LOADER_HEADER}\n", encoding="utf-8")
    installed = {"autosmm": {"name": "AutoSMM", "version": "1.0.1", "stub_path": str(stub)}}
    st_pending_load = core.inspect_plugin_state(cardinal, plugin_info, installed, {})
    assert st_pending_load["status_code"] == "restart_install"
    assert st_pending_load["needs_restart"] is True

    # Loaded older version 1.0 while disk holds 1.0.1 -> restart_update
    cardinal.plugins[target_uuid] = SimpleNamespace(
        name="AutoSMM",
        version="1.0",
        uuid=target_uuid,
        path="plugins/AutoSMM.py",
        enabled=True,
        commands={"autosmm": "Панель AutoSMM"},
        settings_page=False,
    )
    st_pending_upd = core.inspect_plugin_state(cardinal, plugin_info, installed, {})
    assert st_pending_upd["status_code"] == "restart_update"
    assert st_pending_upd["needs_restart"] is True

    # Matching version & enabled -> active with commands
    cardinal.plugins[target_uuid].version = "1.0.1"
    st_active = core.inspect_plugin_state(cardinal, plugin_info, installed, {})
    assert st_active["status_code"] == "active"
    assert st_active["needs_restart"] is False
    assert st_active["commands"] == [("autosmm", "Панель AutoSMM")]

    # Disabled in Cardinal
    cardinal.plugins[target_uuid].enabled = False
    st_disabled = core.inspect_plugin_state(cardinal, plugin_info, installed, {})
    assert st_disabled["status_code"] == "disabled"

    # Uninstalled from Hub & stub removed while still in Cardinal RAM -> restart_delete
    stub.unlink()
    st_pending_del = core.inspect_plugin_state(cardinal, plugin_info, {}, {})
    assert st_pending_del["status_code"] == "restart_delete"
    assert st_pending_del["needs_restart"] is True


def test_invoke_plugin_command_dispatches_and_blocks_disabled(isolated_hub: Path) -> None:
    cardinal = make_fake_cardinal()
    received: list[Any] = []
    cardinal.telegram.bot.message_handlers = [
        {"function": lambda msg: received.append((msg.text, msg.from_user.id)), "filters": {"commands": ["autosmm"]}}
    ]
    loaded_plugin = SimpleNamespace(enabled=False, commands={"autosmm": "Open AutoSMM"})
    base_msg = SimpleNamespace(chat=SimpleNamespace(id=777), text="menu", content_type="text")
    caller = SimpleNamespace(id=42, username="owner")

    ok_disabled, err_disabled = core.invoke_plugin_command(cardinal, loaded_plugin, base_msg, caller)
    assert ok_disabled is False
    assert "отключён" in err_disabled
    assert received == []

    loaded_plugin.enabled = True
    ok, cmd = core.invoke_plugin_command(cardinal, loaded_plugin, base_msg, caller)
    assert ok is True
    assert cmd == "autosmm"
    assert received == [("/autosmm", 42)]
    assert base_msg.text == "menu"


def test_generated_loader_supports_future_annotations_dataclass_and_defaults(isolated_hub: Path) -> None:
    import sys

    plugin_dir = isolated_hub / "plugins" / "w1bbex" / "DataclassPlugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "dc_plugin.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Item:\n"
        "    title: str\n"
        "    qty: int = 1\n",
        encoding="utf-8",
    )

    loader_a = installer.generate_loader("dc_plugin", "DataclassPlugin", "DataclassPlugin", "DC", "1.0", "Desc")
    loader_b = installer.generate_loader("dc_plugin", "DataclassPlugin", "DataclassPlugin", "DC", "1.0", "Desc")

    mods = []
    for idx, lpath in enumerate((loader_a, loader_b)):
        mod_name = f"test_dc_stub_{idx}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, lpath)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mods.append(mod)
        finally:
            sys.modules.pop(mod_name, None)
            sys.modules.pop("DataclassPlugin", None)

    assert mods[0].Item(title="ok").qty == 1
    assert mods[0].UUID == mods[1].UUID
    assert hasattr(mods[0], "BIND_TO_DELETE")


def test_download_plugin_rejects_broken_py_and_sha256_mismatch(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    runtime.PLUGINS_CACHE["items"] = [
        {"id": "demo", "name": "Demo", "latest_version": "1.0", "is_free": True, "sha256": hashlib.sha256(b"NAME = 'Demo'\n").hexdigest()}
    ]
    runtime.PLUGINS_CACHE["fetched_at"] = 9e12

    bad_html = MagicMock(status_code=200, headers={"Content-Type": "text/plain", "X-Filename": "demo.py"}, content=b"<html>bad</html>")
    bad_sha_resp = MagicMock(
        status_code=200,
        headers={"Content-Type": "text/plain", "X-Filename": "demo.py"},
        content=b"NAME = 'Tampered'\n",
    )
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: bad_sha_resp)
    ok_sha, msg_sha = installer.download_plugin(make_fake_cardinal(), "demo", None, SimpleNamespace(id=1, username="u"))
    assert ok_sha is False
    assert "SHA-256" in msg_sha

    runtime.PLUGINS_CACHE["items"][0].pop("sha256")
    monkeypatch.setattr(installer.requests, "post", lambda *a, **kw: bad_html)
    ok_py, _ = installer.download_plugin(make_fake_cardinal(), "demo", None, SimpleNamespace(id=1, username="u"))
    assert ok_py is False
    assert not (isolated_hub / "plugins" / "Demo.py").exists()


def test_is_newer_version_prerelease_fallback_without_packaging(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_packaging(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "packaging.version":
            raise ImportError("missing packaging")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_packaging)
    assert core.is_newer_version("1.0.0", "1.0.0a1") is True
    assert core.is_newer_version("1.0.0a1", "1.0.0") is False
    assert core.is_newer_version("1.0.1", "1.0.0") is True
    assert core.is_newer_version("0.3.0", "0.3.0") is False


def test_generated_loader_fallback_uuid_is_valid_rfc4122_v4(isolated_hub: Path) -> None:
    import importlib.util
    import sys
    import uuid

    plugin_dir = isolated_hub / "plugins" / "w1bbex" / "NoUuidPlugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "no_uuid.py").write_text(
        "NAME = 'NoUuid'\nVERSION = '1.0'\nDESCRIPTION = 'desc'\nCREDITS = '@w1bbex'\nDELETE_TO_PLUGIN = False\n",
        encoding="utf-8",
    )

    loader_a = Path(installer.generate_loader("no_uuid", "NoUuidPlugin", "NoUuidPlugin", "NoUuid", "1.0", "desc"))
    loader_b = Path(installer.generate_loader("no_uuid", "NoUuidPlugin", "NoUuidPlugin", "NoUuid", "1.0", "desc"))
    content = loader_a.read_text(encoding="utf-8")
    assert "import uuid" not in content

    mods = []
    for idx, lpath in enumerate((loader_a, loader_b)):
        mod_name = f"test_loader_uuid4_{idx}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, lpath)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mods.append(mod)
        finally:
            sys.modules.pop(mod_name, None)
            sys.modules.pop("NoUuidPlugin", None)

    parsed = uuid.UUID(mods[0].UUID, version=4)
    assert str(parsed) == mods[0].UUID
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122
    assert mods[0].UUID == mods[1].UUID
