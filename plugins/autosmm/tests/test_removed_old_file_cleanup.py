import ast
from pathlib import Path
import types
import unittest


SETTINGS_SOURCE = Path(__file__).resolve().parent.parent / "src" / "tg" / "settings.py"
FILES_SOURCE = Path(__file__).resolve().parent.parent / "src" / "tg" / "files.py"


class OldFileCleanupTests(unittest.TestCase):
    def test_settings_opens_without_scanning_plugin_files(self):
        tree = ast.parse(SETTINGS_SOURCE.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_smm_settings")
        function.returns = None
        for arg in function.args.args:
            arg.annotation = None
        events = []
        namespace = {
            "_telemetry_gate_allows_menu": lambda: True,
            "_open_main_settings_menu": lambda chat_id: events.append(("menu", chat_id)),
            "_maybe_send_telemetry_ping": lambda *args, **kwargs: events.append(("telemetry",)),
            "rt": types.SimpleNamespace(cardinal_instance=object()),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(SETTINGS_SOURCE), "exec"), namespace)
        namespace["auto_smm_settings"](types.SimpleNamespace(chat=types.SimpleNamespace(id=42)))
        self.assertEqual(events, [("menu", 42), ("telemetry",)])

    def test_cleanup_menu_retains_data_cleanup_only(self):
        tree = ast.parse(FILES_SOURCE.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "autosmm_cleanup_menu")
        strings = [node.value for node in ast.walk(function) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        self.assertIn("full_autosmm_delete_prompt", strings)
        self.assertFalse(any("cleanup_old_files" in value for value in strings))


if __name__ == "__main__":
    unittest.main()
