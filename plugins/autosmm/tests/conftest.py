"""Pytest isolation fixtures for AutoSMM tests."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import socket
import sys

import pytest

AUTOSMM_ROOT = Path(__file__).resolve().parent.parent
if str(AUTOSMM_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOSMM_ROOT))

from src import runtime as rt

_ORIG_RESOLVE_CARDINAL_ROOT = rt._resolve_cardinal_root
_ORIG_GETADDRINFO = socket.getaddrinfo


def _offline_test_getaddrinfo(host, port, *args, **kwargs):
    host_norm = str(host or "").strip().strip(".").lower()
    if host_norm in ("example", "invalid", "test") or host_norm.endswith((".example", ".invalid", ".test")):
        raise socket.gaierror(-2, "Offline test domain")
    return _ORIG_GETADDRINFO(host, port, *args, **kwargs)


def _is_under_root(path_val: Path, root_val: Path) -> bool:
    try:
        path_val.resolve().relative_to(root_val.resolve())
        return True
    except ValueError:
        return False


@pytest.fixture(scope="session", autouse=True)
def session_autosmm_isolation(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point default AutoSMM runtime paths to an isolated session directory."""
    cardinal_root = tmp_path_factory.mktemp("autosmm_cardinal_session")
    plugins_dir = cardinal_root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    stub_entry = plugins_dir / "AutoSMM.py"
    stub_entry.write_text("# test stub\n", encoding="utf-8")
    rt.configure_paths(stub_entry)
    return cardinal_root


@pytest.fixture(autouse=True)
def isolated_autosmm_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate AutoSMM storage paths, logger file handlers, and runtime state per test."""
    cardinal_root = tmp_path / "cardinal"
    plugins_dir = cardinal_root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    stub_entry = plugins_dir / "AutoSMM.py"
    stub_entry.write_text("# test stub\n", encoding="utf-8")

    def _isolated_resolve_cardinal_root(entry: Path) -> Path:
        resolved_entry = Path(entry).resolve()
        if _is_under_root(resolved_entry, AUTOSMM_ROOT):
            return cardinal_root
        return _ORIG_RESOLVE_CARDINAL_ROOT(resolved_entry)

    monkeypatch.setattr(socket, "getaddrinfo", _offline_test_getaddrinfo)
    monkeypatch.setattr(rt, "_resolve_cardinal_root", _isolated_resolve_cardinal_root)
    rt.configure_paths(stub_entry)
    rt.RUNNING = False
    if hasattr(rt, "STOP_EVENT"):
        rt.STOP_EVENT.clear()
    rt.clear_memory_contexts()
    if hasattr(rt, "invalidate_panel_balance_cache"):
        rt.invalidate_panel_balance_cache()

    yield cardinal_root

    rt.RUNNING = False
    if hasattr(rt, "STOP_EVENT"):
        rt.STOP_EVENT.set()
    fe = sys.modules.get("src.core.funpay_events")
    if fe is not None and hasattr(fe, "stop_event_workers"):
        fe.stop_event_workers(drain=False, timeout=2.0)
    if hasattr(rt, "join_background_threads"):
        rt.join_background_threads(timeout=2.0)
    if hasattr(rt, "STOP_EVENT"):
        rt.STOP_EVENT.clear()
    rt.clear_memory_contexts()
    tmp_resolved = tmp_path.resolve()
    for handler in list(rt.logger.handlers):
        if isinstance(handler, logging.FileHandler):
            base_fn = getattr(handler, "baseFilename", None)
            if base_fn and _is_under_root(Path(os.path.abspath(base_fn)), tmp_resolved):
                try:
                    handler.flush()
                    handler.close()
                except Exception:
                    pass
                try:
                    rt.logger.removeHandler(handler)
                except ValueError:
                    pass
