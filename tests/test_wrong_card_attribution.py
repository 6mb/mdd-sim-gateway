"""A mis-bound reader must never be charged to the line's exit node.

A line opening another line's SIM arrives on the failover path looking exactly like an exit
failure: the tunnel is up, IMS registration is refused, the line freezes. Attributed to the
exit it walks the whole candidate pool and tears down sibling lines' working tunnels, while
the actual fault — the binding — goes unmentioned for as long as anyone watches containers
rebuild. These tests pin the attribution down.
"""
import unittest
from unittest.mock import MagicMock, patch

from control.app import failover, main


OURS = "8901260444723809824"
THEIRS = "8944303773524072104"
BOUND = "VoWiFi Modem 2c7c-0125-4-1 00 00"
OTHER = "VoWiFi Modem 2c7c-0125-1-1 00 00"


class LocalCardFaultTests(unittest.TestCase):
    def _fault(self, pin=None, swu=None, usim=None, inst=None):
        files = {"pin_status.json": pin or {}, "swu_status.json": swu or {},
                 "usim_status.json": usim or {}}
        with patch.object(main.engine, "read_run_json",
                          side_effect=lambda iid, name: files.get(name)):
            return main._local_card_fault("1", inst or {})

    def test_a_healthy_line_reports_no_local_fault(self):
        self.assertEqual(
            self._fault(pin={"state": "PIN_DISABLED", "reader": BOUND},
                        swu={"state": "CONNECTED"}, usim={"state": "AUTH_OK"},
                        inst={"pin_reader": BOUND}),
            "")

    def test_pin_keeper_refusal_is_reported_verbatim(self):
        detail = f"reader holds ICCID {THEIRS}, this line is {OURS}"
        self.assertEqual(self._fault(pin={"state": "WRONG_CARD", "detail": detail}), detail)

    def test_ims_path_refusal_is_reported(self):
        detail = f"reader holds ICCID {THEIRS}, this line is {OURS}"
        self.assertEqual(self._fault(usim={"state": "WRONG_CARD", "detail": detail}), detail)

    def test_tunnel_refusal_falls_back_to_the_iccid_it_recorded(self):
        fault = self._fault(swu={"state": "WRONG_CARD", "iccid": THEIRS})
        self.assertIn(THEIRS, fault)

    def test_opening_a_reader_other_than_the_bound_one_is_a_local_fault(self):
        fault = self._fault(pin={"state": "PIN_DISABLED", "reader": OTHER},
                            inst={"pin_reader": BOUND})
        self.assertIn(OTHER, fault)
        self.assertIn(BOUND, fault)

    def test_a_line_on_its_own_reader_is_not_accused(self):
        self.assertEqual(
            self._fault(pin={"state": "PIN_DISABLED", "reader": BOUND},
                        inst={"pin_reader": BOUND}),
            "")

    def test_search_specs_name_a_search_not_a_slot_so_they_are_not_compared(self):
        # imsi:/iccid:/numeric bindings resolve to whichever reader holds the card, so the
        # opened name legitimately differs from the stored spec.
        for spec in ("imsi:310260123456789", "iccid:" + OURS, "0"):
            with self.subTest(spec=spec):
                self.assertEqual(
                    self._fault(pin={"state": "PIN_DISABLED", "reader": OTHER},
                                inst={"pin_reader": spec}),
                    "")

    def test_sw_9862_is_read_as_a_mis_bound_reader(self):
        fault = self._fault(usim={"state": "AUTH_FAIL", "detail": "sw=9862"})
        self.assertIn("9862", fault)

    def test_other_authentication_failures_are_left_to_the_exit_policy(self):
        self.assertEqual(self._fault(usim={"state": "AUTH_FAIL", "detail": "sw=6982"}), "")


class ExitAttributionTests(unittest.TestCase):
    def test_a_local_card_fault_holds_and_never_reaches_the_exit_policy(self):
        with patch.object(main, "_local_card_fault", return_value="the reader holds 894430…"), \
                patch.object(main, "egress", MagicMock()) as egress, \
                patch.object(main, "failover", MagicMock(HOLD=failover.HOLD)) as policy:
            action = main._judge_exit_failure("1", {}, {"reason_code": "reg_rejected"}, 0.0)
        self.assertEqual(action, failover.HOLD)
        egress.status.assert_not_called()
        policy.classify.assert_not_called()
        policy.record.assert_not_called()

    def test_without_a_local_fault_the_exit_policy_still_decides(self):
        with patch.object(main, "_local_card_fault", return_value=""), \
                patch.object(main, "egress", MagicMock()) as egress, \
                patch.object(main, "engine", MagicMock()), \
                patch.object(main, "_peer_line_registered", return_value=False), \
                patch.object(main, "_save_exit_ledgers"), \
                patch.object(main, "failover", MagicMock(
                    HOLD=failover.HOLD, SWITCH=failover.SWITCH, GIVE_UP=failover.GIVE_UP,
                    REPORT=failover.REPORT, BACK_OFF=failover.BACK_OFF)) as policy:
            egress.status.return_value = {"exits": {}}
            policy.record.return_value = (failover.HOLD, {})
            main._judge_exit_failure("1", {}, {"reason_code": "reg_rejected"}, 0.0)
        policy.classify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
