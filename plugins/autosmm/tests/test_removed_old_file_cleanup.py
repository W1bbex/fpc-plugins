from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import runtime as rt
from src.tg import files, settings


class CleanupMenuBehaviorTests(unittest.TestCase):
    def test_settings_opens_without_scanning_plugin_files(self):
        events = []
        message = SimpleNamespace(chat=SimpleNamespace(id=42))
        with (
            mock.patch.object(settings, "_telemetry_gate_allows_menu", return_value=True),
            mock.patch.object(
                settings,
                "_open_main_settings_menu",
                side_effect=lambda chat_id: events.append(("menu", chat_id)),
            ),
            mock.patch.object(
                settings,
                "_maybe_send_telemetry_ping",
                side_effect=lambda *args, **kwargs: events.append(("telemetry",)),
            ),
            mock.patch.object(rt, "cardinal_instance", object()),
        ):
            settings.auto_smm_settings(message)
        self.assertEqual(events, [("menu", 42), ("telemetry",)])

    def test_cleanup_menu_retains_data_cleanup_only(self):
        edited = []
        fake_bot = SimpleNamespace(
            edit_message_text=lambda text, chat_id, message_id, **kwargs: edited.append(
                (text, chat_id, message_id, kwargs)
            ),
            answer_callback_query=lambda *args, **kwargs: None,
        )
        call = SimpleNamespace(
            id="cb-1",
            from_user=SimpleNamespace(id=100),
            message=SimpleNamespace(chat=SimpleNamespace(id=200), message_id=300),
        )
        with (
            mock.patch.object(rt, "bot", fake_bot),
            mock.patch.object(files, "_is_authorized_tg_user", return_value=True),
            mock.patch.object(files, "_safe_answer_callback"),
        ):
            files.autosmm_cleanup_menu(call)

        self.assertEqual(len(edited), 1)
        text, chat_id, msg_id, kwargs = edited[0]
        self.assertEqual((chat_id, msg_id), (200, 300))
        markup = kwargs.get("reply_markup")
        callback_values = [
            btn.callback_data
            for row in (getattr(markup, "keyboard", None) or [])
            for btn in row
            if getattr(btn, "callback_data", None)
        ]
        self.assertIn("full_autosmm_delete_prompt", callback_values)
        self.assertFalse(any("cleanup_old_files" in cb for cb in callback_values))
        self.assertNotIn("cleanup_old_files", text)


if __name__ == "__main__":
    unittest.main()
