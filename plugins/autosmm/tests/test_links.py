import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core import links


class LinksTests(unittest.TestCase):
    def test_target_and_domain_allowlist_without_plugin_module(self):
        domains = lambda: ["t.me"]
        self.assertEqual(links._links_extract_buyer_target("https://t.me/channel", allowed_domains=domains), "https://t.me/channel")
        self.assertTrue(links._links_is_valid_link("https://t.me/channel", allowed_domains=domains)[0])
        self.assertFalse(links._links_is_valid_link("https://t.me.attacker.example/channel", allowed_domains=domains)[0])
        self.assertTrue(links._links_is_valid_buyer_target("@channel", allowed_domains=domains)[0])
        self.assertFalse(links._links_is_valid_buyer_target("@bad!", allowed_domains=domains)[0])
        self.assertEqual(links._links_extract_http_link("https://t.me/channel", allowed_domains=domains), "https://t.me/channel")

    def test_domain_normalization_and_validation(self):
        self.assertEqual(links._normalize_allowed_domain("https://www.Example.com/a"), "example.com")
        self.assertTrue(links._is_plausible_allowed_domain("example.com"))
        self.assertFalse(links._is_plausible_allowed_domain("example..com"))

    def test_target_format_depends_on_lot(self):
        self.assertEqual(links._format_target_for_panel("@channel", {"name": "Telegram", "link_format": "full_url"}), "https://t.me/channel")
        self.assertEqual(links._format_target_for_panel("https://t.me/channel", {"link_format": "username_at"}), "@channel")


class LinksNormalizationTests(unittest.TestCase):
    def test_mixed_case_url_schemes_are_normalized_and_accepted(self):
        from src import models

        allowed = lambda: ["t.me"]
        extracted = links._links_extract_buyer_target("Https://t.me/my_channel", allowed_domains=allowed)
        self.assertEqual(extracted, "https://t.me/my_channel")
        ok_link, _msg_link = links._links_is_valid_link("HTTPS://T.ME/my_channel", allowed_domains=allowed)
        self.assertTrue(ok_link)
        ok_target, _msg_target = links._links_is_valid_buyer_target("Https://t.me/my_channel", allowed_domains=allowed)
        self.assertTrue(ok_target)
        self.assertEqual(models.normalize_allowed_domain("https://[::bad"), "")

