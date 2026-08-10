"""One-click self-update: control-plane request publishing + host updater file handling."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config, update_check

_SPEC = importlib.util.spec_from_file_location(
    "mdd_update", Path(__file__).resolve().parent.parent / "host" / "mdd_update.py")
mdd_update = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mdd_update)

_AVAILABLE = {"ok": True, "update_available": True, "latest": "9.9.9",
              "release_url": "https://example.invalid/release"}


class RequestApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request_path = os.path.join(self.tmp.name, "orchestrator", "update-request.json")
        self.status_path = os.path.join(self.tmp.name, "orchestrator", "update-status.json")

    def test_request_is_published_with_version_and_repository(self):
        with patch.object(update_check, "check", return_value=dict(_AVAILABLE)):
            result = update_check.request_apply()
        self.assertTrue(result["ok"])
        with open(self.request_path, encoding="utf-8") as handle:
            request = json.load(handle)
        self.assertEqual(request["version"], "9.9.9")
        self.assertEqual(request["repository"], update_check.repository())
        with open(self.status_path, encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["phase"], "requested")

    def test_no_available_update_publishes_nothing(self):
        with patch.object(update_check, "check",
                          return_value={"ok": True, "update_available": False}):
            result = update_check.request_apply()
        self.assertFalse(result["ok"])
        self.assertFalse(os.path.exists(self.request_path))

    def test_running_update_is_not_requested_twice(self):
        os.makedirs(os.path.dirname(self.status_path))
        with open(self.status_path, "w", encoding="utf-8") as handle:
            json.dump({"state": "running", "phase": "reloading",
                       "updated_at": int(time.time())}, handle)
        with patch.object(update_check, "check", return_value=dict(_AVAILABLE)):
            result = update_check.request_apply()
        self.assertEqual(result["error_code"], "update.error.in_progress")
        self.assertFalse(os.path.exists(self.request_path))

    def test_unconsumed_request_is_reported_as_stalled(self):
        os.makedirs(os.path.dirname(self.request_path))
        with open(self.request_path, "w", encoding="utf-8") as handle:
            json.dump({"version": "9.9.9", "requested_at": int(time.time()) - 300}, handle)
        status = update_check.apply_status()
        self.assertTrue(status["requested"])
        self.assertEqual(status["state"], "stalled")


class UpdaterTests(unittest.TestCase):
    def test_apply_tree_preserves_installation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, source = Path(tmp, "repo"), Path(tmp, "source")
            for relative in ["data/auth.json", ".git/config", "control/.venv/bin/python",
                            "control/app/stale.py", "webui/node_modules/pkg/index.js",
                            "webui/dist/index.html", "README.md"]:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old", encoding="utf-8")
            (repo / ".env").write_text("MDD_PORT=9999", encoding="utf-8")
            for relative in ["control/app/new.py", "webui/src/App.jsx", "README.md", "VERSION"]:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("new", encoding="utf-8")

            mdd_update.apply_tree(source, repo)

            self.assertEqual((repo / "data/auth.json").read_text(encoding="utf-8"), "old")
            self.assertEqual((repo / ".env").read_text(encoding="utf-8"), "MDD_PORT=9999")
            self.assertEqual((repo / ".git/config").read_text(encoding="utf-8"), "old")
            self.assertEqual((repo / "control/.venv/bin/python").read_text(encoding="utf-8"), "old")
            self.assertTrue((repo / "webui/node_modules/pkg/index.js").exists())
            self.assertEqual((repo / "webui/dist/index.html").read_text(encoding="utf-8"), "old",
                             "the served dist must survive so a failed rebuild keeps the UI up")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "new")
            self.assertEqual((repo / "control/app/new.py").read_text(encoding="utf-8"), "new")
            self.assertFalse((repo / "control/app/stale.py").exists(),
                             "files removed upstream must not linger in managed directories")

    def test_perform_rejects_malformed_version_and_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = mdd_update.Status(Path(tmp, "status.json"), "x")
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform(Path(tmp), Path(tmp), "../evil", "MddIdd/mdd-sim-gateway", status)
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform(Path(tmp), Path(tmp), "1.0.2", "MddIdd/x/../y", status)


if __name__ == "__main__":
    unittest.main()
