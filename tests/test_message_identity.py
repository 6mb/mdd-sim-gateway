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


if __name__ == "__main__":
    unittest.main()
