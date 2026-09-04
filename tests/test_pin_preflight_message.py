"""The PIN-preflight 409 must always carry a human-readable detail.message.

Issue #60: without it the WebUI's generic error path falls back to the bare HTTP
status text and the operator only sees "能力切换失败: Conflict" with no way to know
the line needs a PIN (or that the reader holds another card/eSIM profile).
"""
import unittest

from control.app import main


class PinPreflightMessageTests(unittest.TestCase):
    def test_every_code_carries_message(self):
        for pf in ({"ok": False, "code": "no_card"},
                   {"ok": False, "code": "pin_required", "tries": 3},
                   {"ok": False, "code": "pin_invalid", "clear": True, "tries": 2}):
            exc = main._pin_preflight_http(pf)
            self.assertEqual(exc.status_code, 409)
            self.assertEqual(exc.detail["code"], pf["code"])
            self.assertEqual(exc.detail.get("tries"), pf.get("tries"))
            self.assertTrue(exc.detail.get("message"), pf["code"])

    def test_tries_are_rendered_when_known(self):
        exc = main._pin_preflight_http({"ok": False, "code": "pin_invalid", "tries": 1})
        self.assertIn("1 tries left", exc.detail["message"])
        exc = main._pin_preflight_http({"ok": False, "code": "pin_required"})
        self.assertNotIn("tries left", exc.detail["message"])

    def test_unknown_code_still_readable(self):
        exc = main._pin_preflight_http({"ok": False, "code": "mystery"})
        self.assertIn("mystery", exc.detail["message"])
