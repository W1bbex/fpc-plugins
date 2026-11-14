"""Shared fixtures and fake Cardinal/Telegram helpers for W1bbex Hub tests."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

HUB_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = HUB_ROOT.parent.parent
if str(HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(HUB_ROOT))

from src import runtime


@pytest.fixture()
def isolated_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point W1bbex Hub runtime paths and cache to an isolated Cardinal directory."""
    cardinal_root = tmp_path / "cardinal"
    (cardinal_root / "plugins").mkdir(parents=True, exist_ok=True)
    hub_file = cardinal_root / "plugins" / "W1bbexHub.py"
    hub_file.write_text("# test stub", encoding="utf-8")

    runtime.configure_paths(anchor_file=hub_file, cardinal_root=cardinal_root)
    runtime.PLUGINS_CACHE["items"] = []
    runtime.PLUGINS_CACHE["fetched_at"] = 0.0
    runtime.PLUGINS_CACHE["last_attempt_at"] = 0.0
    runtime.PLUGINS_CACHE["checked_at"] = 0.0
    runtime.PLUGINS_CACHE["error"] = False
    runtime.STOP_EVENT.clear()

    yield cardinal_root

    runtime.PLUGINS_CACHE["items"] = []
    runtime.PLUGINS_CACHE["fetched_at"] = 0.0
    runtime.PLUGINS_CACHE["last_attempt_at"] = 0.0
    runtime.PLUGINS_CACHE["checked_at"] = 0.0
    runtime.PLUGINS_CACHE["error"] = False
    loaded_handlers = sys.modules.get("src.handlers")
    if loaded_handlers is not None and hasattr(loaded_handlers, "stop_hub"):
        loaded_handlers.stop_hub()
    runtime.STOP_EVENT.set()


def make_fake_cardinal(fp_id=12345, fp_username="seller_test", authorized_users=None):
    """Create a minimal mock Cardinal object compatible with HubController and helpers."""
    bot = MagicMock()
    bot.send_message.side_effect = lambda chat_id, text, **kw: SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), text=text, message_id=101
    )
    bot.reply_to.side_effect = lambda msg, text, **kw: SimpleNamespace(
        chat=msg.chat, text=text, message_id=102
    )

    cbq_routes: list[tuple] = []
    msg_routes: list[tuple] = []
    user_states: dict[int, dict[int, dict]] = {}

    def set_state(chat_id: int, message_id: int, user_id: int, state: str, data: dict | None = None) -> None:
        user_states.setdefault(chat_id, {})[user_id] = {
            "state": state,
            "mid": message_id,
            "data": data or {},
        }

    def get_state(chat_id: int, user_id: int) -> dict | None:
        return user_states.get(chat_id, {}).get(user_id)

    def clear_state(chat_id: int, user_id: int, del_msg: bool = False) -> None:
        chat_states = user_states.get(chat_id)
        if not chat_states or user_id not in chat_states:
            return
        mid = chat_states[user_id].get("mid")
        del chat_states[user_id]
        if not chat_states:
            del user_states[chat_id]
        if del_msg and mid is not None:
            bot.delete_message(chat_id, mid)

    def check_state(chat_id: int, user_id: int, state: str) -> bool:
        entry = get_state(chat_id, user_id)
        return bool(entry and entry.get("state") == state)

    tg = SimpleNamespace(
        bot=bot,
        authorized_users=authorized_users,
        cbq_handler=lambda handler, func=None, **kw: cbq_routes.append((handler, func)),
        msg_handler=lambda handler, **kw: msg_routes.append((handler, kw)),
        cbq_routes=cbq_routes,
        msg_routes=msg_routes,
        user_states=user_states,
        set_state=set_state,
        get_state=get_state,
        clear_state=clear_state,
        check_state=check_state,
    )

    account = None
    if fp_id is not None or fp_username is not None:
        account = SimpleNamespace(id=fp_id, user_id=fp_id, username=fp_username)

    return SimpleNamespace(
        telegram=tg,
        account=account,
        add_telegram_commands=MagicMock(),
    )
