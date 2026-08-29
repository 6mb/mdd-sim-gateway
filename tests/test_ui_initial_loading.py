"""Regression coverage for truthful first-render states in Calls and Devices."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOFTPHONE = (ROOT / "webui/src/views/Softphone.jsx").read_text(encoding="utf-8")
DEVICES = (ROOT / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")


class InitialLoadingUiTests(unittest.TestCase):
    def test_softphone_does_not_report_unregistered_before_first_registration(self):
        self.assertIn("const [reg, setReg] = useState('loading')", SOFTPHONE)
        self.assertIn("registeredOnce.current ? 'unregistered' : 'connecting'", SOFTPHONE)
        self.assertIn("registeredOnce.current = true", SOFTPHONE)

    def test_softphone_disabled_notice_waits_for_provisioning(self):
        self.assertIn("prov && !prov.enabled", SOFTPHONE)
        self.assertNotIn("!prov?.enabled", SOFTPHONE)

    def test_device_pages_wait_for_the_first_hardware_scan(self):
        self.assertIn("const pending = discovering", DEVICES)
        self.assertIn("if (discovering) return <Discovering t={t} />", DEVICES)


if __name__ == "__main__":
    unittest.main()
