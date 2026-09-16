import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import store


class TempStore:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "mdd-sim-gateway.sqlite"
        self.patch = patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(self.db),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.temp.cleanup()


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.ctx = TempStore().__enter__()
        store.init()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_redelivery_with_same_network_timestamp_is_ignored(self):
        first = store.ingest_message("1", "in", "+447700900123", "hello", transport="cellular",
                                     sent_ts=1_000, received_ts=1_002)
        again = store.ingest_message("1", "in", "+447700900123", "hello", transport="cellular",
                                     sent_ts=1_000, received_ts=1_500)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(first["ts"], 1_000)
        self.assertEqual(first["received_ts"], 1_002)

    def test_copy_over_other_transport_is_folded_even_in_national_format(self):
        store.ingest_message("1", "in", "+447700900123", "code 1234", transport="vowifi",
                             sent_ts=1_000, received_ts=1_001)
        copy = store.ingest_message("1", "in", "07700900123", "code 1234",
                                    transport="cellular", sent_ts=1_004)
        self.assertIsNone(copy)
        self.assertEqual(len(store.list_threads("1")), 1)

    def test_same_text_later_is_a_new_message(self):
        store.ingest_message("1", "in", "+447700900123", "ok", transport="vowifi", sent_ts=1_000)
        later = store.ingest_message("1", "in", "+447700900123", "ok", transport="cellular",
                                     sent_ts=5_000)
        same_transport = store.ingest_message("1", "in", "+447700900123", "ok",
                                              transport="vowifi", sent_ts=1_001)
        self.assertIsNotNone(later)
        self.assertIsNotNone(same_transport)

    def test_deleted_message_is_not_resurrected(self):
        rec = store.ingest_message("1", "in", "INFO", "promo", transport="cellular",
                                   sent_ts=2_000)
        store.delete_messages("1", [rec["id"]])
        self.assertIsNone(store.ingest_message("1", "in", "INFO", "promo",
                                               transport="cellular", sent_ts=2_000))

    def test_outgoing_object_without_timestamp_has_content_identity(self):
        first = store.ingest_message("1", "out", "+447700900123", "hi", transport="cellular",
                                     received_ts=10_000)
        again = store.ingest_message("1", "out", "+447700900123", "hi", transport="cellular",
                                     received_ts=99_000)
        self.assertIsNotNone(first)
        self.assertIsNone(again)

    def test_implausible_future_network_time_falls_back_to_receipt(self):
        rec = store.ingest_message("1", "in", "+447700900123", "x", transport="vowifi",
                                   sent_ts=10**10, received_ts=1_000)
        self.assertEqual(rec["ts"], 1_000)
        self.assertIsNone(rec["sent_ts"])

    def test_grown_body_keeps_both_identities(self):
        rec = store.ingest_message("1", "in", "+447700900123", "part one[…]",
                                   transport="vowifi", sent_ts=3_000)
        store.set_message_body(rec["id"], "part one part two")
        self.assertIsNone(store.ingest_message("1", "in", "+447700900123", "part one part two",
                                               transport="cellular", sent_ts=3_001))
        self.assertIsNone(store.ingest_message("1", "in", "+447700900123", "part one[…]",
                                               transport="vowifi", sent_ts=3_000))


class IdentityMigrationTests(unittest.TestCase):
    def test_upgrade_backfills_identity_and_folds_existing_duplicates(self):
        with TempStore() as ctx:
            with sqlite3.connect(ctx.db) as db:
                db.executescript("""
                    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        instance TEXT NOT NULL, direction TEXT NOT NULL, peer TEXT NOT NULL,
                        body TEXT NOT NULL, status TEXT DEFAULT 'ok', ts INTEGER NOT NULL,
                        error TEXT, transport TEXT DEFAULT 'vowifi');
                    CREATE TABLE message_imports (fingerprint TEXT PRIMARY KEY,
                        instance TEXT NOT NULL, imported_ts INTEGER NOT NULL);
                    INSERT INTO messages(instance,direction,peer,body,ts,transport) VALUES
                        ('1','in','+447700900123','?',1000,'vowifi'),
                        ('1','in','+447700900123','?',1000,'cellular'),
                        ('1','in','+447700900123','?',1000,'cellular'),
                        ('1','in','99','--',1500,'cellular'),
                        ('1','in','SHOP','code 1',2000,'cellular'),
                        ('1','out','+447700900123','again',3000,'vowifi'),
                        ('1','out','+447700900123','again',3001,'vowifi');
                    INSERT INTO message_imports VALUES ('old','1',1);
                """)
            store.init()
            store.init()  # a second start must not re-run the data step
            with sqlite3.connect(ctx.db) as db:
                rows = db.execute("SELECT peer,body,transport FROM messages ORDER BY id").fetchall()
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(rows, [
                ("+447700900123", "?", "vowifi"),
                ("SHOP", "code 1", "cellular"),
                ("+447700900123", "again", "vowifi"),
                ("+447700900123", "again", "vowifi"),
            ])
            self.assertGreaterEqual(version, 1)
            self.assertNotIn("message_imports", tables)
            # The modem still holds the imported text: the first poll after upgrade must not
            # import it again.
            self.assertIsNone(store.ingest_message("1", "in", "SHOP", "code 1",
                                                   transport="cellular", sent_ts=2000))


class SubscriberScopeTests(unittest.TestCase):
    def setUp(self):
        self.ctx = TempStore().__enter__()
        self.lines = {"1": "iccid:8900000000000000001"}
        store.set_subscriber_resolver(lambda iid: self.lines.get(iid, ""))
        store.init()

    def tearDown(self):
        store.set_subscriber_resolver(None)
        self.ctx.__exit__(None, None, None)

    def test_same_sim_under_a_new_line_id_keeps_its_identities(self):
        rec = store.ingest_message("1", "in", "INFO", "kept on modem", transport="cellular",
                                   sent_ts=5_000)
        store.delete_messages("1", [rec["id"]])
        del self.lines["1"]
        self.lines["4"] = "iccid:8900000000000000001"       # line deleted, SIM re-added
        self.assertIsNone(store.ingest_message("4", "in", "INFO", "kept on modem",
                                               transport="cellular", sent_ts=5_000))

    def test_another_sim_reusing_a_line_id_starts_clean(self):
        store.ingest_message("1", "in", "INFO", "same text", transport="cellular", sent_ts=5_000)
        self.lines["1"] = "iccid:8900000000000000002"
        self.assertIsNotNone(store.ingest_message("1", "in", "INFO", "same text",
                                                  transport="cellular", sent_ts=5_000))

    def test_line_without_sim_identity_is_scoped_to_its_id(self):
        store.ingest_message("9", "in", "INFO", "x", transport="vowifi", sent_ts=5_000)
        with store._conn() as c:
            self.assertEqual(c.execute("SELECT scope FROM message_identities WHERE instance='9'")
                             .fetchone()[0], "line:9")


class ScopeMigrationTests(unittest.TestCase):
    def test_version_three_identities_move_to_their_sim(self):
        with TempStore() as ctx:
            store.init()
            store.ingest_message("1", "in", "INFO", "old", transport="cellular", sent_ts=5_000)
            with sqlite3.connect(ctx.db) as db:
                # Rebuild the pre-scope table as a version 3 database had it.
                rows = db.execute("SELECT instance,fingerprint,content_hash,transport,ts,"
                                  "message_id,created_ts FROM message_identities").fetchall()
                db.executescript("""
                    DROP TABLE message_identities;
                    CREATE TABLE message_identities (instance TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                        transport TEXT NOT NULL, ts INTEGER NOT NULL, message_id INTEGER,
                        created_ts INTEGER NOT NULL, PRIMARY KEY(instance, fingerprint));
                    CREATE INDEX idx_message_identities_content
                        ON message_identities(instance, content_hash, ts);
                    PRAGMA user_version=3;
                """)
                db.executemany("INSERT INTO message_identities VALUES(?,?,?,?,?,?,?)", rows)
            store.set_subscriber_resolver(lambda iid: {"1": "imsi:001010000000001"}.get(iid, ""))
            try:
                store.init()
                with store._conn() as c:
                    self.assertEqual([r[0] for r in c.execute("SELECT scope FROM message_identities")],
                                     ["imsi:001010000000001"])
                self.assertIsNone(store.ingest_message("1", "in", "INFO", "old",
                                                       transport="cellular", sent_ts=5_000))
            finally:
                store.set_subscriber_resolver(None)


class TimeZoneIndependenceTests(unittest.TestCase):
    ZONES = ("UTC", "Asia/Shanghai", "America/Los_Angeles", "Europe/Berlin")

    def each_zone(self, fn):
        import os, time
        original = os.environ.get("TZ")
        results = set()
        try:
            for zone in self.ZONES:
                os.environ["TZ"] = zone
                time.tzset()
                results.add(fn())
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()
        return results

    def test_modemmanager_timestamps_do_not_depend_on_the_host_zone(self):
        from control.app import cellular_sms
        cases = {"2026-03-14T15:09:26+02": 1773493766, "2026-03-14T15:09:26+02:00": 1773493766,
                 "2026-03-14T15:09:26+0200": 1773493766, "2026-03-14T13:09:26Z": 1773493766,
                 "2026-03-14T09:09:26-04": 1773493766}
        for raw, expected in cases.items():
            self.assertEqual(self.each_zone(lambda: cellular_sms._timestamp(raw)), {expected}, raw)
        self.assertEqual(self.each_zone(lambda: cellular_sms._timestamp("2026-03-14T15:09:26")),
                         {0}, "a zone-less value is refused, not read in local time")

    def test_vowifi_scts_does_not_depend_on_the_host_zone(self):
        from control.app import sms_pdu
        self.assertEqual(self.each_zone(
            lambda: sms_pdu.deliver_timestamp("4404812143000462304151906280")), {1773493766})


if __name__ == "__main__":
    unittest.main()
