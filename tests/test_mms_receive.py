import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import main, mms, mms_pdu as m, store

LOCATION = "http://mmsc.example.test:8002/?id=abc"


def notification_push(location=LOCATION, *, tid="T1", sender="447700900123/TYPE=PLMN",
                      size=24_000) -> bytes:
    sender_value = bytes([0x80]) + m.write_encoded_string_value(sender)
    expiry = bytes([0x81]) + m.write_long_integer(172_800)
    body = (bytes([0x8C, 0x82]) + b"\x98" + m.write_text_string(tid) + b"\x8D\x92"
            + b"\x89" + m.write_value_length(len(sender_value)) + sender_value
            + b"\x8A\x80" + b"\x88" + m.write_value_length(len(expiry)) + expiry
            + b"\x8E" + m.write_long_integer(size) + b"\x83" + m.write_text_string(location))
    return bytes([0x01, 0x06, 0x01, 0xBE]) + body


def delivery_push(message_id: str, status: int = m.STATUS_RETRIEVED) -> bytes:
    body = (bytes([0x8C, 0x86]) + b"\x8D\x92" + b"\x8B" + m.write_text_string(message_id)
            + b"\x97" + m.write_text_string("+447700900123/TYPE=PLMN")
            + b"\x85" + m.write_long_integer(1_800_000_000) + bytes([0x95, status]))
    return bytes([0x02, 0x06, 0x01, 0xBE]) + body


class TempStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.patch = patch.multiple(store, DATA_DIR=str(root),
                                    DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        store.init()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()


class HandleWapPushTests(TempStore):
    def test_notification_becomes_one_pending_mms_in_the_existing_conversation(self):
        store.add_message("1", "in", "+447700900123", "earlier text")
        result = mms.handle_wap_push("1", "99", notification_push(), transport="cellular",
                                     sent_ts=1_000)
        rec = result["message"]
        self.assertTrue(result["handled"])
        self.assertEqual(rec["kind"], "mms")
        self.assertEqual(rec["peer"], "+447700900123")
        self.assertEqual(rec["mms"]["state"], "notified")
        self.assertEqual(rec["mms"]["size"], 24_000)
        self.assertNotIn("content_location", rec["mms"], "the MMSC URL stays server-side")
        self.assertEqual(store.mms_for_download(rec["id"])["content_location"], LOCATION)
        self.assertEqual(len(store.due_mms_downloads()), 1)

        again = mms.handle_wap_push("1", "99", notification_push(), transport="vowifi",
                                    sent_ts=9_000)
        self.assertTrue(again["handled"])
        self.assertIsNone(again["message"], "the same MMS over the other transport")

    def test_non_mms_push_and_garbage_are_not_handled(self):
        other = bytes([0x01, 0x06, 0x01, 0xAE]) + b"\x00\x01"   # application/vnd.wap.sic
        self.assertFalse(mms.handle_wap_push("1", "99", other, transport="vowifi")["handled"])
        self.assertFalse(mms.handle_wap_push("1", "99", b"\xff", transport="vowifi")["handled"])

    def test_delivery_report_marks_outgoing_mms_delivered(self):
        rec = store.create_outgoing_mms("1", "+447700900123", to_addrs=["+447700900123"],
                                        subject="", body="hi", transaction_id="TX")
        store.set_mms_state(rec["id"], "sent", message_ref="MSG-1", message_status="sent")
        result = mms.handle_wap_push("1", "99", delivery_push("MSG-1"), transport="vowifi")
        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertEqual(result["delivery"]["mms"]["state"], "delivered")
        self.assertIn("+447700900123", result["delivery"]["mms"]["delivery"])

    def test_wap_push_udh_detection(self):
        self.assertTrue(mms.is_wap_push_udh("05040b8423f0"))
        self.assertFalse(mms.is_wap_push_udh("0003a70201"))
        self.assertFalse(mms.is_wap_push_udh("zz"))


class PartStorageTests(TempStore):
    def test_parts_are_files_and_deleting_the_message_removes_them(self):
        rec = mms.handle_wap_push("1", "99", notification_push(), transport="vowifi")["message"]
        store.save_mms_content(rec["id"], [
            {"content_type": "text/plain", "data": "你好".encode(), "name": "t.txt",
             "charset": "utf-8", "text": "你好"},
            {"content_type": "image/jpeg", "data": b"\xff\xd8\xff", "name": "../../evil.jpg"},
        ], body="你好", subject="")
        stored = store.get_message(rec["id"])
        self.assertEqual(stored["body"], "你好")
        image = stored["mms"]["parts"][1]
        found = store.mms_part_file("1", rec["id"], image["id"])
        self.assertTrue(found["file"].startswith(os.path.realpath(self.root / "mms")))
        self.assertEqual(Path(found["file"]).read_bytes(), b"\xff\xd8\xff")
        self.assertIsNone(store.mms_part_file("2", rec["id"], image["id"]), "other line")
        store.delete_messages("1", [rec["id"]])
        self.assertFalse(os.path.exists(os.path.dirname(found["file"])))
        self.assertIsNone(store.mms_for_download(rec["id"]))


class InboundEventTests(unittest.IsolatedAsyncioTestCase, TempStore):
    def setUp(self):
        TempStore.setUp(self)
        self.broadcast = AsyncMock()
        self.push = Mock()
        for target, attr, new in ((main.hub, "broadcast", self.broadcast),
                                  (main, "_dispatch_push", self.push)):
            p = patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        TempStore.tearDown(self)

    @staticmethod
    def event(payload: bytes, triplet=("", "", ""), udh="05040b8423f0"):
        widened = payload.decode("latin-1")
        return {"instance": "1", "event": "sms_in", "args": [
            "99", base64.b64encode(widened.encode()).decode(), *map(str, triplet),
            "0", "4", udh, ""]}

    async def test_vowifi_wap_push_is_stored_as_mms_not_filed(self):
        result = await main.api_engine_event(self.event(notification_push()))
        self.assertEqual(result.get("stored"), "mms")
        self.assertEqual(store.list_binary_sms("1"), [])
        threads = store.list_threads("1")
        self.assertEqual(threads[0]["last_kind"], "mms")
        self.assertEqual(self.push.call_count, 1)
        self.assertTrue(self.push.call_args[0][3].startswith("[MMS]"))

    async def test_long_wap_push_is_reassembled_from_its_parts(self):
        payload = notification_push()
        first, second = payload[:20], payload[20:]
        udh = "0003070201" + "05040b8423f0"
        r1 = await main.api_engine_event(self.event(first, (7, 2, 1), udh))
        self.assertIn("buffered", r1)
        await main.api_engine_event(self.event(second, (7, 2, 2), udh))
        self.assertEqual(len(store.due_mms_downloads()), 1)

    async def test_other_binary_payload_is_still_filed(self):
        result = await main.api_engine_event(self.event(b"\x00\x01\x02\x7f", udh=""))
        self.assertEqual(result.get("stored"), "binary")
        self.assertEqual(self.push.call_count, 0)


if __name__ == "__main__":
    unittest.main()
