import json
import unittest

from control.app import cellular_sms


class Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class CellularSmsTests(unittest.TestCase):
    def test_received_sms_is_mapped_to_instance_by_iccid(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"
        sms = "/org/freedesktop/ModemManager1/SMS/7"
        responses = {
            ("mmcli", "-L"): Result(modem),
            ("mmcli", "-m", modem, "--output-json"): Result(json.dumps({
                "modem": {"generic": {"sim": sim}}})),
            ("mmcli", "-i", sim, "--output-json"): Result(json.dumps({
                "sim": {"properties": {"iccid": "card-a"}}})),
            ("mmcli", "-m", modem, "--messaging-list-sms", "--output-json"): Result(
                json.dumps({"modem.messaging.sms": [sms]})),
            ("mmcli", "-s", sms, "--output-json"): Result(json.dumps({"sms": {
                "content": {"number": "+44123", "text": "hello"},
                "properties": {"pdu-type": "deliver", "timestamp": "2026-08-03T00:00:00+08:00"},
            }})),
        }

        def runner(args, **_kwargs):
            return responses.get(tuple(args), Result(returncode=1))

        rows = cellular_sms.discover([{"id": "3", "iccid": "card-a"}], runner=runner)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance"], "3")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual(rows[0]["transport"], "cellular")

    def test_unknown_sim_and_empty_body_are_ignored(self):
        self.assertEqual(cellular_sms.discover([], runner=lambda *_a, **_k: Result()), [])

    def test_scanner_caches_topology_and_sms_details_but_keeps_listing_live(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"
        sms = "/org/freedesktop/ModemManager1/SMS/7"
        calls = []
        responses = {
            ("mmcli", "-L"): Result(modem),
            ("mmcli", "-m", modem, "--output-json"): Result(json.dumps({
                "modem": {"generic": {"sim": sim}}})),
            ("mmcli", "-i", sim, "--output-json"): Result(json.dumps({
                "sim": {"properties": {"iccid": "card-a"}}})),
            ("mmcli", "-m", modem, "--messaging-list-sms", "--output-json"): Result(
                json.dumps({"modem.messaging.sms": [sms]})),
            ("mmcli", "-s", sms, "--output-json"): Result(json.dumps({"sms": {
                "content": {"number": "+44123", "text": "hello"},
                "properties": {"pdu-type": "deliver", "timestamp": "2026-08-03T00:00:00+08:00"},
            }})),
        }

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            return responses.get(tuple(args), Result(returncode=1))

        now = [10.0]
        scanner = cellular_sms.Scanner(runner, clock=lambda: now[0])
        first = scanner.discover([{"id": "3", "iccid": "card-a"}])
        now[0] += 5
        second = scanner.discover([{"id": "3", "iccid": "card-a"}])

        self.assertEqual(first, second)
        self.assertEqual(calls.count(("mmcli", "-L")), 1)
        self.assertEqual(calls.count(("mmcli", "-m", modem, "--output-json")), 1)
        self.assertEqual(calls.count(("mmcli", "-i", sim, "--output-json")), 1)
        self.assertEqual(calls.count(("mmcli", "-s", sms, "--output-json")), 1)
        self.assertEqual(calls.count(
            ("mmcli", "-m", modem, "--messaging-list-sms", "--output-json")), 2)

    def test_scanner_refreshes_stable_objects_after_ttl(self):
        modem = "/org/freedesktop/ModemManager1/Modem/0"
        sim = "/org/freedesktop/ModemManager1/SIM/0"
        calls = []

        def runner(args, **_kwargs):
            calls.append(tuple(args))
            if args == ["mmcli", "-L"]:
                return Result(modem)
            if args[:3] == ["mmcli", "-m", modem] and "--messaging-list-sms" not in args:
                return Result(json.dumps({"modem": {"generic": {"sim": sim}}}))
            if args[:3] == ["mmcli", "-i", sim]:
                return Result(json.dumps({"sim": {"properties": {"iccid": "card-a"}}}))
            if "--messaging-list-sms" in args:
                return Result(json.dumps({"modem.messaging.sms": []}))
            return Result(returncode=1)

        now = [10.0]
        scanner = cellular_sms.Scanner(runner, topology_ttl=60, clock=lambda: now[0])
        scanner.discover([{"id": "3", "iccid": "card-a"}])
        now[0] = 71.0
        scanner.discover([{"id": "3", "iccid": "card-a"}])
        self.assertEqual(calls.count(("mmcli", "-L")), 2)


if __name__ == "__main__":
    unittest.main()
