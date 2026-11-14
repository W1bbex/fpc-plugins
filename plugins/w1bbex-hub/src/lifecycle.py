"""Background update scheduler, post-restart reporter, and Hub lifecycle hooks."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from . import hub_api
from .catalog import catalog_status
from .core import get_disk_hub_pending_version, inspect_plugin_state, is_newer_version
from .hub_updates import notify_hub_update
from .manual_updates import include_manual_installs, notification_chat
from .runtime import (
    RESTART_NOTICE_TTL_SECONDS,
    STOP_EVENT,
    UPDATE_CHECK_INITIAL_DELAY_SECONDS,
    UPDATE_WAKE_EVENT,
    VERSION,
    configure_file_logging,
    logger,
)
from .storage import (
    clear_pending_restart,
    load_data,
    peek_pending_restart,
    process_pending_deletions,
    save_pending_restart,
    update_data,
)
from .transport import _call_tg_html
from .ui import esc, render_main_menu, render_post_restart_report, render_update_notification


POST_RESTART_FALLBACK_DELAY_SECONDS = 8.0

_UPDATE_THREAD_LOCK = threading.Lock()
_UPDATE_THREAD: Optional[threading.Thread] = None
_UPDATE_STOP_EVENT: Optional[threading.Event] = None
_UPDATE_WAKE_EVENT: Optional[threading.Event] = None
_POST_RESTART_LOCK = threading.Lock()
_POST_RESTART_ACTIVE = False
_POST_RESTART_TIMER: Optional[threading.Timer] = None
_POST_INIT_EVENT = threading.Event()
_POST_RESTART_WAKE_EVENT = threading.Event()
_FALLBACK_THREAD_LOCK = threading.Lock()
_FALLBACK_THREAD: Optional[threading.Thread] = None
_FALLBACK_STOP_EVENT: Optional[threading.Event] = None


def wake_update_checker() -> None:
    """Wake both legacy global wake event and the active reloadable update loop."""
    UPDATE_WAKE_EVENT.set()
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_WAKE_EVENT is not None:
            _UPDATE_WAKE_EVENT.set()


def _save_restart_marker(
    action: str,
    chat_id: Any,
    plugin_id: str = "",
    plugin_name: str = "",
    target_version: str = "",
    expected_uuid: str = "",
    stub_path: str = "",
    bin_dir: str = "",
    message_id: Any = None,
) -> None:
    if not chat_id:
        return
    try:
        save_pending_restart(
            {
                "action": action,
                "chat_id": chat_id,
                "message_id": message_id,
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "target_version": target_version,
                "expected_uuid": expected_uuid,
                "stub_path": stub_path,
                "bin_dir": bin_dir,
            }
        )
    except OSError:
        logger.exception("Failed persisting pending_restart state")


def run_update_check_cycle(cardinal: Any) -> int:
    """Check Hub and installed plugins without requiring the owner to open /hub."""
    from .core import collect_pending_updates

    data = load_data()
    if not data.get("auto_check_updates", True):
        return 0
    tg_id = notification_chat(cardinal, data)
    if not tg_id:
        return 0

    if not data.get("notify_updates", True):
        user = type("UserObj", (), {"id": tg_id, "username": ""})()
        hub_api.fetch_hub_latest_version(cardinal, user)
        hub_api.get_plugins()
        return 0

    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None)
    if tg is None or bot is None:
        return 0

    notification_data = {**data, "owner_tg_id": tg_id}
    sent_count = notify_hub_update(cardinal, notification_data)
    plugins = hub_api.get_plugins()
    if not plugins:
        return sent_count

    pending = collect_pending_updates(include_manual_installs(cardinal, plugins, notification_data), plugins, time.time())
    if not pending:
        return sent_count
    notify_enabled = bool(data.get("notify_updates", True))
    for update in pending:
        text, kb = render_update_notification(update, notify_enabled=notify_enabled)
        try:
            _call_tg_html(
                lambda t, rm: bot.send_message(tg_id, t, parse_mode="HTML", reply_markup=rm),
                text,
                reply_markup=kb,
            )
        except Exception as exc:
            logger.error("Failed sending update notification for %s: %s", update["id"], exc)
            continue

        update_key = update["update_key"]
        try:
            update_data(lambda cur, k=update_key: cur.setdefault("notified_updates", {}).__setitem__(k, -1))
            sent_count += 1
        except OSError:
            logger.exception("Failed recording notified update state for %s", update_key)
    return sent_count


def _update_checker_loop(
    cardinal: Any,
    stop_event: threading.Event,
    wake_event: Optional[threading.Event] = None,
) -> None:
    """Daemon loop that sleeps cleanly even when no plugins are installed."""
    wake = wake_event or UPDATE_WAKE_EVENT
    if stop_event.wait(UPDATE_CHECK_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        try:
            run_update_check_cycle(cardinal)
        except Exception:
            logger.exception("Update checker error")
        try:
            data = load_data()
        except OSError:
            logger.exception("Update checker failed reading Hub config")
            data = {}
        raw_interval = data.get("update_check_interval_minutes", 30)
        interval = raw_interval if isinstance(raw_interval, int) and not isinstance(raw_interval, bool) and 5 <= raw_interval <= 10080 else 30
        wake.wait(interval * 60)
        wake.clear()
        if stop_event.is_set():
            break


def process_post_restart_notice(cardinal: Any, retry_delay: float = 2.0) -> bool:
    """Inspect pending_restart, verify Cardinal/disk state, notify the owner, then clear marker."""
    global _POST_RESTART_ACTIVE
    configure_file_logging()

    with _POST_RESTART_LOCK:
        if _POST_RESTART_ACTIVE:
            return False
        try:
            pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
        except OSError:
            logger.exception("Failed reading pending_restart during post_init")
            return False
        if not pending:
            return False
        _POST_RESTART_ACTIVE = True

    try:
        try:
            data = load_data()
            chat_id = pending.get("chat_id") or data.get("owner_tg_id")
            if not chat_id:
                clear_pending_restart()
                return False

            bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
            if bot is None:
                logger.warning("Post-restart Hub report deferred: Telegram bot unavailable")
                return False

            action = str(pending.get("action") or "general")
            pid = str(pending.get("plugin_id") or "")
            installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
            keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}

            state: Optional[dict[str, Any]] = None
            disk_still_exists = False

            if action == "hub":
                target_ver = str(pending.get("target_version") or "")
                pending["hub_mismatch"] = bool(target_ver and is_newer_version(target_ver, VERSION))
            elif pid:
                catalog = hub_api.get_plugins()
                p_info = next((x for x in catalog if x.get("id") == pid), None) or {
                    "id": pid,
                    "name": pending.get("plugin_name") or pid,
                    "uuid": pending.get("expected_uuid") or "",
                    "latest_version": pending.get("target_version") or "",
                }
                state = inspect_plugin_state(cardinal, p_info, installed, keys)
                if action == "delete":
                    disk_still_exists = any(
                        bool(path and os.path.exists(str(path)))
                        for path in (pending.get("stub_path"), pending.get("bin_dir"))
                    )

            if (action == "hub" and not pending.get("hub_mismatch")) or (action == "general" and not pid):
                banner = (
                    f"<b>W1bbex Hub обновлён до v{esc(VERSION)}</b>"
                    if action == "hub"
                    else "<b>FunPay Cardinal перезапущен</b>"
                )
                plugin_states, cur_data = hub_api._build_plugin_states(cardinal)
                cur_keys = cur_data.get("keys") if isinstance(cur_data.get("keys"), dict) else {}
                text, kb = render_main_menu(
                    plugin_states=plugin_states,
                    sys_info=hub_api.get_sys_info(),
                    notify_updates=bool(cur_data.get("notify_updates", True)),
                    hub_latest_version=VERSION,
                    has_hub_update=False,
                    hub_pending_version=get_disk_hub_pending_version(),
                    keys_count=len(cur_keys),
                    notice_banner=banner,
                    catalog_status=catalog_status(),
                )
            else:
                text, kb = render_post_restart_report(
                    pending=pending,
                    state=state,
                    running_hub_version=VERSION,
                    disk_still_exists=disk_still_exists,
                )
        except Exception:
            logger.exception("Failed preparing post-restart Hub report")
            return False

        old_msg_id = pending.get("message_id")
        if old_msg_id:
            try:
                bot.delete_message(chat_id, old_msg_id)
            except Exception:
                logger.debug("Could not delete transient restart message %s", old_msg_id, exc_info=True)

        for attempt in range(2):
            try:
                _call_tg_html(
                    lambda t, rm: bot.send_message(
                        chat_id,
                        t,
                        parse_mode="HTML",
                        reply_markup=rm,
                        disable_web_page_preview=True,
                    ),
                    text,
                    reply_markup=kb,
                )
                try:
                    clear_pending_restart()
                except OSError:
                    logger.exception("Failed clearing pending_restart after successful delivery")
                logger.info("Sent post-restart Hub notice action=%s pid=%s chat_id=%s", action, pid or "-", chat_id)
                return True
            except Exception:
                if attempt == 0 and retry_delay > 0:
                    logger.warning("Retrying post-restart Hub notice delivery to chat %s", chat_id, exc_info=True)
                    time.sleep(retry_delay)
                else:
                    logger.exception("Failed sending post-restart Hub report to chat %s", chat_id)
        return False
    finally:
        with _POST_RESTART_LOCK:
            _POST_RESTART_ACTIVE = False


def _post_restart_fallback_watcher(
    cardinal: Any,
    delay_seconds: float = POST_RESTART_FALLBACK_DELAY_SECONDS,
    post_init_event: Optional[threading.Event] = None,
    wake_event: Optional[threading.Event] = None,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    """Wait briefly for Cardinal BIND_TO_POST_INIT; send notice early if another plugin stalls startup."""
    post_ev = post_init_event or _POST_INIT_EVENT
    wake_ev = wake_event or _POST_RESTART_WAKE_EVENT
    stop_ev = stop_event or STOP_EVENT

    if delay_seconds > 0 and not post_ev.is_set() and not stop_ev.is_set():
        wake_ev.wait(timeout=delay_seconds)

    if stop_ev.is_set() or post_ev.is_set():
        return False

    try:
        pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
    except OSError:
        logger.exception("Failed checking pending_restart in fallback watcher")
        return False
    if not pending:
        return False

    logger.info(
        "BIND_TO_POST_INIT did not reach W1bbex Hub within %.1fs; dispatching post-restart notice from pre_init fallback",
        delay_seconds,
    )
    return process_post_restart_notice(cardinal)


def _arm_post_restart_fallback(cardinal: Any, stop_event: Optional[threading.Event] = None) -> bool:
    """Spawn a daemon fallback watcher when pending_restart exists at pre_init."""
    global _FALLBACK_THREAD, _FALLBACK_STOP_EVENT
    try:
        pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
    except OSError:
        logger.exception("Failed checking pending_restart on pre_init")
        return False
    if not pending:
        return False

    run_stop = stop_event or threading.Event()
    with _FALLBACK_THREAD_LOCK:
        if _FALLBACK_STOP_EVENT is not None:
            _FALLBACK_STOP_EVENT.set()
            _POST_RESTART_WAKE_EVENT.set()
        _FALLBACK_STOP_EVENT = run_stop
        if _FALLBACK_THREAD is not None and _FALLBACK_THREAD.is_alive() and not stop_event:
            return False
        thread = threading.Thread(
            target=_post_restart_fallback_watcher,
            args=(cardinal, POST_RESTART_FALLBACK_DELAY_SECONDS, _POST_INIT_EVENT, _POST_RESTART_WAKE_EVENT, run_stop),
            name="W1bbexHubPostRestartFallback",
            daemon=True,
        )
        _FALLBACK_THREAD = thread
        thread.start()
        logger.info(
            "Armed pre_init post-restart fallback watcher (action=%s pid=%s delay=%.1fs)",
            pending.get("action"),
            pending.get("plugin_id") or "-",
            POST_RESTART_FALLBACK_DELAY_SECONDS,
        )
        return True


def prepare_lifecycle_for_init() -> None:
    """Reset runtime stop/wake flags and clean deferred plugin deletions at pre_init."""
    global _POST_RESTART_TIMER
    configure_file_logging()
    process_pending_deletions()
    with _POST_RESTART_LOCK:
        if _POST_RESTART_TIMER is not None:
            _POST_RESTART_TIMER.cancel()
            _POST_RESTART_TIMER = None
    _POST_INIT_EVENT.clear()
    _POST_RESTART_WAKE_EVENT.clear()
    UPDATE_WAKE_EVENT.clear()
    STOP_EVENT.clear()


def start_lifecycle_workers(cardinal: Any, *, arm_fallback: bool) -> None:
    """Spawn update checker thread and optional post-restart fallback watcher."""
    global _UPDATE_THREAD, _UPDATE_STOP_EVENT, _UPDATE_WAKE_EVENT
    run_stop = threading.Event()
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_STOP_EVENT is not None:
            _UPDATE_STOP_EVENT.set()
        if _UPDATE_WAKE_EVENT is not None:
            _UPDATE_WAKE_EVENT.set()
        can_reuse = _UPDATE_THREAD is not None and _UPDATE_THREAD.is_alive() and _UPDATE_STOP_EVENT is None
        if not can_reuse:
            run_wake = threading.Event()
            _UPDATE_STOP_EVENT = run_stop
            _UPDATE_WAKE_EVENT = run_wake
            thread = threading.Thread(
                target=_update_checker_loop,
                args=(cardinal, run_stop, run_wake),
                name="W1bbexHubUpdateChecker",
                daemon=True,
            )
            _UPDATE_THREAD = thread
            thread.start()
    if arm_fallback:
        _arm_post_restart_fallback(cardinal, stop_event=run_stop)


def stop_hub(*_args: Any, **_kwargs: Any) -> None:
    """Cardinal BIND_TO_DELETE entry point signaling background loops to stop."""
    global _POST_RESTART_TIMER
    STOP_EVENT.set()
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_STOP_EVENT is not None:
            _UPDATE_STOP_EVENT.set()
        if _UPDATE_WAKE_EVENT is not None:
            _UPDATE_WAKE_EVENT.set()
    with _FALLBACK_THREAD_LOCK:
        if _FALLBACK_STOP_EVENT is not None:
            _FALLBACK_STOP_EVENT.set()
    with _POST_RESTART_LOCK:
        if _POST_RESTART_TIMER is not None:
            _POST_RESTART_TIMER.cancel()
            _POST_RESTART_TIMER = None
    UPDATE_WAKE_EVENT.set()
    _POST_RESTART_WAKE_EVENT.set()


def post_init_cardinal(cardinal: Any) -> None:
    """Cardinal BIND_TO_POST_INIT entry point sending verified post-restart UI report."""
    global _POST_RESTART_TIMER
    configure_file_logging()
    _POST_INIT_EVENT.set()
    _POST_RESTART_WAKE_EVENT.set()
    try:
        pending = peek_pending_restart(RESTART_NOTICE_TTL_SECONDS)
    except OSError:
        logger.exception("Failed checking pending_restart on post_init")
        return
    if not pending:
        return
    logger.info(
        "Detected pending_restart on post_init (action=%s pid=%s); scheduling report",
        pending.get("action"),
        pending.get("plugin_id") or "-",
    )

    def _run_notice() -> None:
        global _POST_RESTART_TIMER
        try:
            process_post_restart_notice(cardinal)
        finally:
            with _POST_RESTART_LOCK:
                _POST_RESTART_TIMER = None

    with _POST_RESTART_LOCK:
        if _POST_RESTART_TIMER is not None:
            _POST_RESTART_TIMER.cancel()
        timer = threading.Timer(1.0, _run_notice)
        timer.daemon = True
        _POST_RESTART_TIMER = timer
        timer.start()
