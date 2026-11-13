import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.storage import defaults


class DefaultsTests(unittest.TestCase):
    def test_default_configuration_is_complete_and_independent(self):
        first = defaults.create_default_config()
        second = defaults.create_default_config()
        first["lot_mapping"]["lot_1"] = {"service_id": 1}
        self.assertEqual(second["lot_mapping"], {})
        self.assertFalse(defaults._ensure_config_defaults(second))

    def test_custom_buyer_template_survives_defaults_migration(self):
        config = defaults.create_default_config()
        config["messages"]["after_payment"] = "Моё сообщение"
        defaults._ensure_config_defaults(config)
        self.assertEqual(config["messages"]["after_payment"], "Моё сообщение")
