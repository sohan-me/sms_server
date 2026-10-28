"""The migration runs at app import, so each case boots the real app in a
subprocess against a prepared SQLite file and inspects the result directly.
That is the only way to test the code path production actually takes."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))

# An old database: the table as it existed before channels, including the
# `is_used` column that the current schema no longer knows about.
LEGACY_SCHEMA = """
CREATE TABLE otp_message (
    id INTEGER NOT NULL,
    phone VARCHAR(20) NOT NULL,
    otp_message VARCHAR(200) NOT NULL,
    is_used INTEGER,
    message_pub VARCHAR(16),
    created_at DATETIME,
    updated_at DATETIME,
    revision BIGINT,
    PRIMARY KEY (id)
);
CREATE TABLE otp_revision (id INTEGER NOT NULL, value BIGINT NOT NULL, PRIMARY KEY (id));
"""

OTP_COLUMNS = {
    "id",
    "phone",
    "kind",
    "otp_message",
    "message_pub",
    "sender",
    "sender_number",
    "code",
    "amount",
    "created_at",
    "updated_at",
    "revision",
}


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "legacy.db")

    def legacy_db(self, table_constraints="", extra_sql="", rows=()):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(LEGACY_SCHEMA)
        if table_constraints:
            # SQLite cannot add a constraint afterwards; rebuild via a copy.
            conn.executescript(
                f"""
                ALTER TABLE otp_message RENAME TO otp_message_old;
                CREATE TABLE otp_message (
                    id INTEGER NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    otp_message VARCHAR(200) NOT NULL,
                    is_used INTEGER,
                    message_pub VARCHAR(16),
                    created_at DATETIME,
                    updated_at DATETIME,
                    revision BIGINT,
                    PRIMARY KEY (id){table_constraints}
                );
                INSERT INTO otp_message
                    (id, phone, otp_message, is_used, message_pub, created_at, updated_at, revision)
                SELECT id, phone, otp_message, is_used, message_pub, created_at, updated_at, revision
                FROM otp_message_old;
                DROP TABLE otp_message_old;
                """
            )
        if extra_sql:
            conn.executescript(extra_sql)
        conn.executemany(
            "INSERT INTO otp_message "
            "(id, phone, otp_message, is_used, message_pub, created_at, updated_at, revision) "
            "VALUES (?, ?, ?, 0, 'IVAC', '2026-10-01 10:00:00', '2026-10-01 10:00:00', ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO otp_revision (id, value) VALUES (1, ?)",
            (max([row[-1] for row in rows] or [0]),),
        )
        conn.commit()
        conn.close()

    def bootstrap(self):
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        env["DISABLE_DB_OTP_POLLER"] = "1"
        env.pop("REDIS_URL", None)
        env.pop("SMS_PATTERNS_FILE", None)
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr[-4000:])
        return result

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def columns(self, table="otp_message"):
        return {row[1] for row in self.connect().execute(f"PRAGMA table_info('{table}')")}

    def unique_keys(self, table="otp_message"):
        conn = self.connect()
        keys = []
        for row in conn.execute(f"PRAGMA index_list('{table}')"):
            if not row[2]:
                continue
            columns = tuple(r[2] for r in conn.execute(f"PRAGMA index_info('{row[1]}')"))
            keys.append((columns, row[1]))
        return keys

    def test_legacy_phone_constraint_is_replaced_by_phone_kind(self):
        self.legacy_db(
            table_constraints=", CONSTRAINT otp_message_phone_key UNIQUE (phone)",
            extra_sql="CREATE UNIQUE INDEX ix_otp_message_phone ON otp_message (phone);",
            rows=[(1, "01712345678", "IVAC code 111111", 10)],
        )

        self.bootstrap()

        self.assertTrue(OTP_COLUMNS.issubset(self.columns()))
        self.assertEqual([key for key, _ in self.unique_keys()], [("phone", "kind")])

        conn = self.connect()
        row = conn.execute(
            "SELECT phone, kind, code, revision FROM otp_message"
        ).fetchone()
        self.assertEqual(row, ("01712345678", "signin", "", 10))

        # The whole point: a payment OTP for the same number now coexists.
        conn.execute(
            "INSERT INTO otp_message "
            "(phone, kind, otp_message, message_pub, sender, sender_number, code, revision) "
            "VALUES ('01712345678', 'payment', 'bKash OTP 123456', 'IVAC', 'bKash', 'bKash', '123456', 11)"
        )
        conn.commit()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM otp_message").fetchone()[0], 2
        )

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO otp_message "
                "(phone, kind, otp_message, message_pub, sender, sender_number, code, revision) "
                "VALUES ('01712345678', 'payment', 'duplicate', 'IVAC', '', '', '', 12)"
            )

    def test_legacy_rebuild_drops_columns_the_schema_no_longer_has(self):
        self.legacy_db(
            table_constraints=", CONSTRAINT otp_message_phone_key UNIQUE (phone)",
            rows=[(1, "01712345678", "IVAC code 111111", 4)],
        )

        result = self.bootstrap()

        # The constraint forced the table rebuild, so the obsolete column is
        # gone -- and the rebuild said so instead of dropping it silently.
        self.assertNotIn("is_used", self.columns())
        self.assertIn("is_used", result.stderr)

    def test_legacy_phone_index_alone_does_not_force_a_rebuild(self):
        self.legacy_db(
            extra_sql="CREATE UNIQUE INDEX ix_otp_message_phone ON otp_message (phone);",
            rows=[(1, "01712345678", "IVAC code 111111", 4)],
        )

        self.bootstrap()

        self.assertEqual([key for key, _ in self.unique_keys()], [("phone", "kind")])
        # No constraint, so no rebuild: columns outside the schema survive.
        self.assertIn("is_used", self.columns())

    def test_duplicate_rows_are_collapsed_per_phone_and_kind(self):
        self.legacy_db(
            rows=[
                (1, "01712345678", "IVAC code 111111", 1),
                (2, "01712345678", "IVAC code 222222", 2),
                (3, "01612345678", "IVAC code 333333", 3),
            ],
        )

        self.bootstrap()

        conn = self.connect()
        rows = conn.execute(
            "SELECT id, phone, otp_message FROM otp_message ORDER BY id"
        ).fetchall()
        # The newest row for the repeated number wins; the other number is kept.
        self.assertEqual(
            rows,
            [
                (2, "01712345678", "IVAC code 222222"),
                (3, "01612345678", "IVAC code 333333"),
            ],
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM otp_message WHERE kind='signin'").fetchone()[0],
            2,
        )

    def test_bootstrap_is_idempotent(self):
        self.legacy_db(
            table_constraints=", CONSTRAINT otp_message_phone_key UNIQUE (phone)",
            rows=[(1, "01712345678", "IVAC code 111111", 7)],
        )

        self.bootstrap()
        self.bootstrap()

        self.assertEqual([key for key, _ in self.unique_keys()], [("phone", "kind")])
        conn = self.connect()
        self.assertEqual(
            conn.execute("SELECT id, kind, revision FROM otp_message").fetchall(),
            [(1, "signin", 7)],
        )
        self.assertEqual(
            conn.execute("SELECT value FROM otp_revision WHERE id = 1").fetchone()[0], 7
        )

    def test_fresh_database_gets_the_new_schema(self):
        self.bootstrap()

        self.assertTrue(OTP_COLUMNS.issubset(self.columns()))
        # Exactly one unique key, from the model constraint -- no duplicate
        # index created by the migration on top of it.
        self.assertEqual([key for key, _ in self.unique_keys()], [("phone", "kind")])
        self.assertEqual(
            [key for key, _ in self.unique_keys("payment_transaction")],
            [("phone", "trx_id")],
        )

        conn = self.connect()
        conn.execute(
            "INSERT INTO payment_transaction "
            "(phone, trx_id, amount, sender, sender_number, raw_message) "
            "VALUES ('01712345678', '9HABCDEFG1', '500.00', 'bKash', 'bKash', 'TrxID 9HABCDEFG1')"
        )
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO payment_transaction "
                "(phone, trx_id, amount, sender, sender_number, raw_message) "
                "VALUES ('01712345678', '9HABCDEFG1', '500.00', 'bKash', 'bKash', 'retry')"
            )

    def test_legacy_revision_cursor_is_preserved(self):
        self.legacy_db(
            table_constraints=", CONSTRAINT otp_message_phone_key UNIQUE (phone)",
            rows=[(1, "01712345678", "IVAC code 111111", 42)],
        )

        self.bootstrap()

        conn = self.connect()
        self.assertEqual(
            conn.execute("SELECT revision FROM otp_message").fetchone()[0], 42
        )
        # A counter that lags would re-deliver OTPs, so it must never go back.
        self.assertGreaterEqual(
            conn.execute("SELECT value FROM otp_revision WHERE id = 1").fetchone()[0], 42
        )


if __name__ == "__main__":
    unittest.main()
