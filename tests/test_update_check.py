import io
import json
import urllib.error
import unittest
from unittest.mock import patch

from control.app import update_check


class _Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        update_check._cache = None

    def test_newer_release_is_reported_without_applying_it(self):
        newer = list(update_check._version_tuple(update_check.VERSION))
        newer[-1] += 1
        payload = json.dumps({"tag_name": "v" + ".".join(map(str, newer)),
                              "html_url": "https://example.invalid/release",
                              "published_at": "2026-08-01T00:00:00Z", "body": "notes"}).encode()
        with patch("control.app.update_check.urllib.request.urlopen", return_value=_Response(payload)):
            result = update_check.check(True)
        self.assertTrue(result["update_available"])
        self.assertEqual(result["current"], update_check.VERSION)
        self.assertNotIn("apply", result)

    def test_semantic_comparison(self):
        self.assertGreater(update_check._version_tuple("v1.10.0"), update_check._version_tuple("1.9.9"))

    def test_repository_can_be_overridden_without_changing_the_ui(self):
        self.assertEqual(update_check.repository(), "MddIdd/mdd-sim-gateway")
        with patch.dict("os.environ", {"MDD_UPDATE_REPOSITORY": "example/private"}):
            self.assertEqual(update_check.repository(), "example/private")

    def test_public_release_request_never_sends_a_github_token(self):
        payload = json.dumps({"tag_name": "v1.0.0"}).encode()
        captured = {}

        def open_request(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _Response(payload)

        with patch.dict("os.environ", {"MDD_GITHUB_TOKEN": "must-not-be-used"}), patch(
                "control.app.update_check.urllib.request.urlopen", side_effect=open_request):
            update_check.check(True)
        self.assertIsNone(captured["authorization"])

    def test_private_repository_does_not_prompt_for_authentication(self):
        failure = urllib.error.HTTPError("https://api.github.invalid", 401,
                                         "Requires authentication", {}, None)
        with patch("control.app.update_check.urllib.request.urlopen", side_effect=failure):
            result = update_check.check(True)
        self.assertEqual(result["error_code"], "update.error.no_public_release")
        self.assertNotIn("auth", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
