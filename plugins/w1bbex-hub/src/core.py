"""Core plugin inspection, version comparison, legacy discovery, and update evaluation."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import os
import re
from typing import Any, Callable, Mapping, Optional, Union

from .runtime import LOADER_HEADER, PATHS, UUID as HUB_UUID, VERSION, logger


def _clean_segment(raw: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw or "").strip()).strip("._")


def sanitize_segment(raw: Any, fallback: str) -> str:
    """Sanitize a filename or directory component against path traversal."""
    cleaned = _clean_segment(raw)
    if cleaned and cleaned not in (".", ".."):
        return cleaned
    safe_fallback = _clean_segment(fallback)
    return safe_fallback if (safe_fallback and safe_fallback not in (".", "..")) else "item"


def plugin_label_from_info(plugin_id: str, plugin_info: Optional[dict[str, Any]]) -> str:
    """Derive a safe plugin stub name from catalog metadata or plugin ID."""
    raw_name = str((plugin_info.get("name") if plugin_info else None) or "").split(" ")[0]
    return sanitize_segment(raw_name, sanitize_segment(plugin_id, "plugin").capitalize())


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if latest is strictly greater than current."""
    norm_latest = str(latest or "0").strip().lstrip("vV")
    norm_current = str(current or "0").strip().lstrip("vV")
    if norm_latest == norm_current:
        return False

    try:
        from packaging.version import InvalidVersion, parse as parse_version
    except ImportError:
        InvalidVersion = None  # type: ignore[assignment,misc]
        parse_version = None  # type: ignore[assignment]

    if parse_version is not None:
        try:
            return parse_version(norm_latest) > parse_version(norm_current)
        except InvalidVersion:  # type: ignore[misc]
            pass

    pre_ranks = {"dev": -4, "a": -3, "alpha": -3, "b": -2, "beta": -2, "rc": -1, "c": -1}
    semver_re = re.compile(
        r"^([0-9]+(?:\.[0-9]+)*)(?:[._-]?(dev|alpha|beta|rc|a|b|c)[._-]?([0-9]*))?$",
        re.IGNORECASE,
    )

    def _key(ver: str) -> tuple[Any, ...]:
        m = semver_re.match(ver)
        if m:
            raw_nums = [int(part) for part in m.group(1).split(".")]
            while len(raw_nums) > 1 and raw_nums[-1] == 0:
                raw_nums.pop()
            nums = tuple(raw_nums)
            tag = (m.group(2) or "").lower()
            if not tag:
                return (1, nums, 0, 0)
            return (1, nums, pre_ranks[tag], int(m.group(3) or 0))
        tokens = [
            (0, int(tok)) if tok.isdigit() else (-1, tok.lower())
            for tok in re.split(r"[._-]+", ver)
            if tok
        ]
        return (0, tuple(tokens))

    return _key(norm_latest) > _key(norm_current)


def extract_py_plugin_metadata(file_path: str) -> dict[str, Any]:
    """Safely inspect top-level plugin constants (UUID, CREDITS, VERSION, NAME) via AST."""
    info: dict[str, Any] = {
        "uuid": None,
        "credits": None,
        "version": None,
        "name": None,
        "is_hub_loader": False,
    }
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
    except OSError as exc:
        logger.debug("Skipping unreadable plugin file %s: %s", file_path, exc)
        return info

    first_line = source.splitlines()[0] if source else ""
    info["is_hub_loader"] = LOADER_HEADER in first_line

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return info

    for node in tree.body:
        target_name: Optional[str] = None
        val_node: Optional[ast.AST] = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            val_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            val_node = node.value

        if target_name in ("UUID", "CREDITS", "VERSION", "NAME") and val_node is not None:
            if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                info[target_name.lower()] = val_node.value.strip()
    return info


def _matches_creator(file_credits: Optional[str], expected_credits: Optional[str]) -> bool:
    if not file_credits or not expected_credits:
        return False
    exp = expected_credits.strip().lower().lstrip("@")
    fc = file_credits.strip().lower().lstrip("@")
    return bool(exp and (exp in fc or fc in exp))


def scan_legacy_plugin_files() -> list[tuple[str, str, dict[str, Any]]]:
    """Scan non-loader .py plugin files and extract metadata once per batch."""
    plugins_dir = PATHS.plugins_dir
    if not os.path.isdir(plugins_dir):
        return []
    with os.scandir(plugins_dir) as it:
        entries = sorted(
            (e for e in it if e.is_file() and e.name.lower().endswith(".py")),
            key=lambda e: e.name,
        )
    scanned: list[tuple[str, str, dict[str, Any]]] = []
    for entry in entries:
        meta = extract_py_plugin_metadata(entry.path)
        if meta.get("is_hub_loader"):
            continue
        scanned.append((entry.name, entry.path, meta))
    return scanned


def _iter_matching_legacy_files(
    target_uuid: Optional[str],
    expected_credits: Optional[str] = "@w1bbex",
    exclude_paths: Optional[list[str]] = None,
    legacy_entries: Optional[list[tuple[str, str, dict[str, Any]]]] = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return non-loader .py entries in plugins_dir matching target_uuid and expected_credits."""
    norm_uuid = str(target_uuid or "").strip().lower()
    if not norm_uuid:
        return []

    excluded = {os.path.normcase(os.path.abspath(PATHS.hub_file))}
    for path in exclude_paths or ():
        if path:
            excluded.add(os.path.normcase(os.path.abspath(path)))

    candidates = legacy_entries if legacy_entries is not None else scan_legacy_plugin_files()
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for filename, file_path, meta in candidates:
        if os.path.normcase(os.path.abspath(file_path)) in excluded:
            continue
        if str(meta.get("uuid") or "").strip().lower() != norm_uuid:
            continue
        if expected_credits and not _matches_creator(meta.get("credits"), expected_credits):
            continue
        matches.append((filename, file_path, meta))
    return matches


def find_manual_plugin_files(
    target_uuid: Optional[str],
    expected_credits: Optional[str] = "@w1bbex",
    exclude_paths: Optional[list[str]] = None,
    legacy_entries: Optional[list[tuple[str, str, dict[str, Any]]]] = None,
) -> list[dict[str, str]]:
    """List manual or legacy plugin files in plugins/ matching UUID and creator."""
    return [
        {
            "filename": filename,
            "path": file_path,
            "version": str(meta.get("version") or ""),
            "name": str(meta.get("name") or ""),
        }
        for filename, file_path, meta in _iter_matching_legacy_files(
            target_uuid, expected_credits, exclude_paths, legacy_entries=legacy_entries
        )
    ]


@dataclass(slots=True, frozen=True)
class PluginUpdateDTO:
    """Strongly-typed data transfer object for plugin and Hub release notifications."""

    id: str
    name: str
    latest: str
    curr_ver: Optional[str] = None
    urgency: str = "medium"
    changelog: str = "Без описания"
    update_key: Optional[str] = None
    target: str = "plugin"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PluginUpdateDTO":
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            latest=str(raw.get("latest") or ""),
            curr_ver=str(raw.get("curr_ver")) if raw.get("curr_ver") is not None else None,
            urgency=str(raw.get("urgency") or "medium"),
            changelog=str(raw.get("changelog") or "Без описания"),
            update_key=str(raw.get("update_key")) if raw.get("update_key") is not None else None,
            target=str(raw.get("target") or "plugin"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "latest": self.latest,
            "curr_ver": self.curr_ver,
            "urgency": self.urgency,
            "changelog": self.changelog,
            "update_key": self.update_key,
            "target": self.target,
        }

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as ex:
            raise KeyError(key) from ex

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def collect_pending_updates(data: dict[str, Any], plugins: list[dict[str, Any]], now: float) -> list[PluginUpdateDTO]:
    """Compute installed plugin updates that are eligible for notification."""
    installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
    notified = data.get("notified_updates") if isinstance(data.get("notified_updates"), dict) else {}
    pending: list[PluginUpdateDTO] = []

    for p in plugins:
        pid = p.get("id", "")
        entry = installed.get(pid)
        if not isinstance(entry, dict) or not p.get("notify_users", True):
            continue
        curr_ver = str(entry.get("version", "v1.0"))
        latest = str(p.get("latest_version", "v1.0"))
        if not is_newer_version(latest, curr_ver):
            continue

        update_key = f"{pid}_{latest}"
        remind_at = notified.get(update_key) if notified.get(update_key) is not None else notified.get(pid)
        if remind_at == -1 or (isinstance(remind_at, (int, float)) and now < remind_at):
            continue

        pending.append(
            PluginUpdateDTO(
                id=pid,
                name=str(p.get("name", pid)),
                latest=latest,
                curr_ver=curr_ver,
                urgency=str(p.get("urgency", "medium")),
                changelog=str(p.get("changelog") or "Без описания"),
                update_key=update_key,
                target="plugin",
            )
        )
    return pending


def _versions_differ(ver_a: Optional[str], ver_b: Optional[str]) -> bool:
    if not ver_a or not ver_b:
        return False
    return is_newer_version(ver_a, ver_b) or is_newer_version(ver_b, ver_a)


def _normalize_plugin_path(raw_path: Optional[str]) -> str:
    if not raw_path:
        return ""
    resolved = raw_path if os.path.isabs(raw_path) else os.path.join(PATHS.cardinal_root, raw_path)
    return os.path.normcase(os.path.abspath(resolved))


def get_disk_hub_pending_version() -> Optional[str]:
    """Return newer Hub version written on disk awaiting Cardinal restart, if any."""
    if not PATHS.hub_file or not os.path.isfile(PATHS.hub_file):
        return None
    meta = extract_py_plugin_metadata(PATHS.hub_file)
    disk_ver = meta.get("version")
    if disk_ver and is_newer_version(str(disk_ver), VERSION):
        return str(disk_ver)
    return None


def find_loaded_plugin(
    cardinal: Any,
    plugin_info: Optional[dict[str, Any]],
    installed_entry: Optional[dict[str, Any]],
) -> tuple[Optional[str], Optional[Any]]:
    """Locate a loaded PluginData instance in cardinal.plugins by UUID, stub path, or exact name."""
    plugins_map = getattr(cardinal, "plugins", None)
    if not isinstance(plugins_map, dict):
        return None, None

    hub_uuid = HUB_UUID.strip().lower()
    target_uuid = str((plugin_info or {}).get("uuid") or "").strip().lower()
    if target_uuid and target_uuid != hub_uuid:
        for uid, pdata in plugins_map.items():
            uid_str = str(uid).strip().lower()
            pdata_uuid = str(getattr(pdata, "uuid", "") or "").strip().lower()
            if uid_str == target_uuid or pdata_uuid == target_uuid:
                return str(uid), pdata

    stub_norm = _normalize_plugin_path((installed_entry or {}).get("stub_path"))
    if stub_norm:
        for uid, pdata in plugins_map.items():
            if str(uid).strip().lower() == hub_uuid:
                continue
            if _normalize_plugin_path(getattr(pdata, "path", None)) == stub_norm:
                return str(uid), pdata

    target_name = str((plugin_info or {}).get("name") or "").strip().lower()
    if target_name:
        for uid, pdata in plugins_map.items():
            if str(uid).strip().lower() == hub_uuid:
                continue
            if str(getattr(pdata, "name", "") or "").strip().lower() == target_name:
                return str(uid), pdata

    return None, None


def _inspect_loaded_plugin_attributes(
    cardinal: Any,
    plugin_info: dict[str, Any],
    installed_entry: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Extract runtime registration, commands, and file paths for a loaded Cardinal plugin."""
    loaded_uuid, loaded_plugin = find_loaded_plugin(cardinal, plugin_info, installed_entry)
    loaded_version = (str(getattr(loaded_plugin, "version", "") or "") or None) if loaded_plugin else None
    loaded_enabled = bool(getattr(loaded_plugin, "enabled", True)) if loaded_plugin else False
    loaded_path = _normalize_plugin_path(getattr(loaded_plugin, "path", None)) if loaded_plugin else ""
    loaded_file_exists = bool(loaded_path and os.path.isfile(loaded_path))
    raw_cmds = getattr(loaded_plugin, "commands", None) if loaded_plugin else None
    commands = [(str(cmd), str(desc)) for cmd, desc in raw_cmds.items()] if isinstance(raw_cmds, dict) else []
    settings_page = bool(getattr(loaded_plugin, "settings_page", False)) if loaded_plugin else False
    return {
        "loaded_uuid": loaded_uuid,
        "loaded_plugin": loaded_plugin,
        "loaded_version": loaded_version,
        "loaded_enabled": loaded_enabled,
        "loaded_path": loaded_path,
        "loaded_file_exists": loaded_file_exists,
        "commands": commands,
        "settings_page": settings_page,
    }


def _resolve_installed_entry_status(
    stub_exists: bool,
    plugins_registry_present: bool,
    loaded_plugin: Any,
    installed_version: Optional[str],
    loaded_version: Optional[str],
    loaded_path: str,
    stub_path: str,
    loaded_enabled: bool,
) -> tuple[str, bool, str]:
    """Determine lifecycle status code and restart requirements for an installed plugin entry."""
    if not stub_exists:
        return "broken_files", False, "general"
    if not plugins_registry_present:
        return "installed", False, "general"
    if loaded_plugin is None:
        return "restart_install", True, "install"
    if _versions_differ(installed_version, loaded_version):
        return "restart_update", True, "update"
    if loaded_path != _normalize_plugin_path(stub_path):
        return "restart_install", True, "install"
    if not loaded_enabled:
        return "disabled", False, "general"
    return "active", False, "general"


def _resolve_uninstalled_entry_status(
    loaded_plugin: Any,
    loaded_file_exists: bool,
    manual_files: list,
) -> tuple[str, bool, str]:
    """Determine lifecycle status code and restart requirements when no installed entry is tracked."""
    if loaded_plugin is not None:
        if loaded_file_exists:
            return "manual", False, "general"
        return "restart_delete", True, "delete"
    if manual_files:
        return "manual", False, "general"
    return "not_installed", False, "general"


def _resolve_manual_version(
    plugin_info: dict[str, Any],
    manual_files: list,
    loaded_version: Optional[str],
    loaded_uuid: Optional[str],
    loaded_path: str,
) -> Optional[str]:
    """Resolve single or loaded manual installation version for a plugin."""
    manual_versions = {item["version"] for item in manual_files if item.get("version")}
    matching_paths = {_normalize_plugin_path(item["path"]) for item in manual_files}
    matched_loaded = bool(
        loaded_version
        and (
            str(loaded_uuid or "").lower() == str(plugin_info.get("uuid") or "").lower()
            or loaded_path in matching_paths
        )
    )
    if matched_loaded:
        return loaded_version
    return next(iter(manual_versions)) if len(manual_versions) == 1 else None


def _extract_installed_entry_fields(
    installed: Optional[dict[str, Any]],
    pid: str,
) -> tuple[Optional[dict[str, Any]], Optional[str], str, bool, str]:
    """Unpack installed map entry, version, stub path, existence, and bin_dir."""
    installed_map = installed if isinstance(installed, dict) else {}
    raw_entry = installed_map.get(pid)
    installed_entry = raw_entry if isinstance(raw_entry, dict) else None
    if not installed_entry:
        return None, None, "", False, ""
    installed_version = str(installed_entry.get("version", "")) or None
    stub_path = str(installed_entry.get("stub_path") or "")
    stub_exists = bool(stub_path and os.path.isfile(stub_path))
    bin_dir = str(installed_entry.get("bin_dir") or "")
    return installed_entry, installed_version, stub_path, stub_exists, bin_dir


def _extract_key_status(keys: Optional[dict[str, Any]], pid: str) -> tuple[bool, str]:
    """Extract key presence and masked display string for a plugin."""
    keys_map = keys if isinstance(keys, dict) else {}
    key_val = str(keys_map.get(pid) or "")
    if not key_val:
        return False, ""
    masked_key = f"{key_val[:6]}…{key_val[-4:]}" if len(key_val) > 12 else key_val
    return True, masked_key


def inspect_plugin_state(
    cardinal: Any,
    plugin_info: dict[str, Any],
    installed: Optional[dict[str, Any]] = None,
    keys: Optional[dict[str, Any]] = None,
    legacy_entries: Optional[list[tuple[str, str, dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Build a complete read-only status snapshot for one catalog plugin."""
    pid = str(plugin_info.get("id", ""))
    name = str(plugin_info.get("name", pid))
    latest_version = str(plugin_info.get("latest_version", "v1.0"))
    is_free = bool(plugin_info.get("is_free", False))

    (
        installed_entry,
        installed_version,
        stub_path,
        stub_exists,
        bin_dir,
    ) = _extract_installed_entry_fields(installed, pid)

    plugins_registry_present = isinstance(getattr(cardinal, "plugins", None), dict)
    loaded_attrs = _inspect_loaded_plugin_attributes(cardinal, plugin_info, installed_entry)

    exclude = [stub_path] if stub_path else []
    manual_files = find_manual_plugin_files(
        plugin_info.get("uuid"),
        plugin_info.get("credits") or "@w1bbex",
        exclude_paths=exclude,
        legacy_entries=legacy_entries,
    )
    plugin_label = plugin_label_from_info(pid, plugin_info)
    storage_dir = os.path.join(PATHS.cardinal_root, "storage", "w1bbex", sanitize_segment(plugin_label, pid))
    storage_dir_exists = os.path.isdir(storage_dir)

    has_key, masked_key = _extract_key_status(keys, pid)

    if installed_entry is not None:
        status_code, needs_restart, restart_action = _resolve_installed_entry_status(
            stub_exists,
            plugins_registry_present,
            loaded_attrs["loaded_plugin"],
            installed_version,
            loaded_attrs["loaded_version"],
            loaded_attrs["loaded_path"],
            stub_path,
            loaded_attrs["loaded_enabled"],
        )
    else:
        status_code, needs_restart, restart_action = _resolve_uninstalled_entry_status(
            loaded_attrs["loaded_plugin"],
            loaded_attrs["loaded_file_exists"],
            manual_files,
        )

    manual_version = _resolve_manual_version(
        plugin_info,
        manual_files,
        loaded_attrs["loaded_version"],
        loaded_attrs["loaded_uuid"],
        loaded_attrs["loaded_path"],
    )
    current_version = installed_version if installed_entry is not None else (manual_version if status_code == "manual" else None)
    catalog_known = bool(plugin_info.get("catalog_known", True))
    has_update = bool(current_version and catalog_known and is_newer_version(latest_version, current_version))

    return {
        "id": pid,
        "name": name,
        "plugin_info": plugin_info,
        "latest_version": latest_version,
        "is_free": is_free,
        "description": str(plugin_info.get("description") or ""),
        "status_code": status_code,
        "needs_restart": needs_restart,
        "restart_action": restart_action,
        "installed_entry": installed_entry,
        "installed_version": installed_version,
        "stub_path": stub_path,
        "stub_exists": stub_exists,
        "bin_dir": bin_dir,
        "loaded_uuid": loaded_attrs["loaded_uuid"],
        "loaded_version": loaded_attrs["loaded_version"],
        "loaded_enabled": loaded_attrs["loaded_enabled"],
        "loaded_file_exists": loaded_attrs["loaded_file_exists"],
        "commands": loaded_attrs["commands"],
        "settings_page": loaded_attrs["settings_page"],
        "manual_files": manual_files,
        "manual_version": manual_version,
        "storage_dir_exists": storage_dir_exists,
        "has_key": has_key,
        "masked_key": masked_key,
        "has_update": has_update,
        "catalog_known": catalog_known,
    }


def invoke_plugin_command(
    cardinal: Any,
    loaded_plugin: Any,
    base_message: Any,
    from_user: Any,
) -> tuple[bool, str]:
    """Dispatch the plugin's primary slash command via Cardinal's registered bot message handlers."""
    if not getattr(loaded_plugin, "enabled", True):
        return False, "Плагин отключён в настройках Cardinal — сначала включите его."

    raw_commands = getattr(loaded_plugin, "commands", None)
    if not isinstance(raw_commands, dict) or not raw_commands:
        return False, "У этого плагина нет зарегистрированной Telegram-команды."

    cmd = str(next(iter(raw_commands.keys()))).lstrip("/").strip()
    bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
    handlers = getattr(bot, "message_handlers", None) if bot else None
    if not isinstance(handlers, list):
        return False, f"Отправьте команду /{cmd} вручную в чат."

    target_fn: Optional[Callable[[Any], Any]] = None
    for entry in handlers:
        if not isinstance(entry, dict):
            continue
        filters = entry.get("filters") or {}
        cmds = filters.get("commands")
        if not cmds:
            continue
        norm_cmds = {str(c).lstrip("/").lower() for c in cmds}
        if cmd.lower() in norm_cmds and callable(entry.get("function")):
            target_fn = entry["function"]
            break

    if target_fn is None:
        return False, f"Обработчик команды /{cmd} не найден. Отправьте /{cmd} в чат."

    synth_msg = copy.copy(base_message)
    synth_msg.text = f"/{cmd}"
    synth_msg.content_type = "text"
    if from_user is not None:
        synth_msg.from_user = from_user

    target_fn(synth_msg)
    return True, cmd
