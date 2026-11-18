"""AC18（結合）: archived 列のスキーマと旧スキーマDBの移行。一時ファイルDBのみ使う。"""
import os
import sqlite3
import tempfile
import unittest

from notes import db, service

OLD_SCHEMA = db.SCHEMA.replace(",\n    archived INTEGER NOT NULL DEFAULT 0", "")


def make_old_db(path):
    raw = sqlite3.connect(path, isolation_level=None)
    raw.executescript(OLD_SCHEMA)
    raw.execute("INSERT INTO users (id, org_id, name, role, api_key) VALUES (1, 10, 'alice', 'member', 'ak-a')")
    raw.execute("INSERT INTO users (id, org_id, name, role, api_key) VALUES (3, 10, 'root', 'admin', 'ak-r')")
    raw.execute("INSERT INTO notes (id, org_id, owner_id, title, body, version, updated_at) "
                "VALUES (5, 10, 1, 'old', 'body', 4, 123)")
    raw.execute("INSERT INTO note_tags (note_id, tag) VALUES (5, 'x')")
    raw.close()


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_v14_new_schema_column_definition(self):
        conn = db.connect(self.path)
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(notes)")}
        c = cols["archived"]
        self.assertEqual((c["type"], c["notnull"], c["dflt_value"]), ("INTEGER", 1, "0"))
        conn.close()

    def test_v14_old_schema_is_really_old(self):
        make_old_db(self.path)
        raw = sqlite3.connect(self.path)
        self.assertNotIn("archived", [r[1] for r in raw.execute("PRAGMA table_info(notes)")])
        raw.close()

    def test_v14_v15_migration_keeps_notes_and_is_idempotent(self):
        make_old_db(self.path)
        for _ in range(2):  # 2回 connect しても壊れない
            conn = db.connect(self.path)
            c = {r["name"]: r for r in conn.execute("PRAGMA table_info(notes)")}["archived"]
            self.assertEqual((c["type"], c["notnull"], c["dflt_value"]), ("INTEGER", 1, "0"))
            r = conn.execute("SELECT * FROM notes WHERE id = 5").fetchone()
            self.assertEqual((r["title"], r["body"], r["version"], r["archived"]), ("old", "body", 4, 0))
            conn.close()
        conn = db.connect(self.path)
        alice = db.get_user(conn, 1)
        self.assertEqual(service.get_note(conn, alice, 5)["archived"], 0)
        self.assertEqual([i["id"] for i in service.list_notes(conn, alice)["items"]], [5])
        self.assertEqual(service.archive_note(conn, alice, 5), 5)
        self.assertEqual(service.list_notes(conn, alice)["total"], 0)
        conn.close()

    def test_v15_transfer_and_delete_archived_after_migration(self):
        make_old_db(self.path)
        conn = db.connect(self.path)
        alice, admin = db.get_user(conn, 1), db.get_user(conn, 3)
        bob = db.add_user(conn, 2, 10, "bob")
        service.archive_note(conn, alice, 5)
        self.assertEqual(service.transfer_notes(conn, admin, alice, bob), 1)
        r = conn.execute("SELECT owner_id, version, archived FROM notes WHERE id = 5").fetchone()
        self.assertEqual(tuple(r), (2, 6, 1))
        service.delete_note(conn, bob, 5)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        conn.close()

    def test_v14_concurrent_migration_duplicate_column_is_ignored(self):
        """別接続が先に列を足した場合（duplicate column）は無視し、列が無いままの失敗は隠さない。"""
        make_old_db(self.path)
        migrated = db.connect(self.path)  # 移行済みの接続。これを使って競合を模擬する

        class Proxy:
            def __init__(self, hide_column):
                self.hide, self.pragma_calls = hide_column, 0

            def execute(self, sql, *a):
                if sql.startswith("PRAGMA table_info"):
                    self.pragma_calls += 1
                    if self.hide and (self.pragma_calls == 1 or self.hide == "always"):
                        return []  # 列が見えない
                if sql.startswith("ALTER TABLE"):
                    raise sqlite3.OperationalError("duplicate column name: archived")
                return migrated.execute(sql, *a)

        db._migrate(Proxy("first"))  # 再確認で列が見えるので無視される
        with self.assertRaises(sqlite3.OperationalError):
            db._migrate(Proxy("always"))  # 本当に列が無いなら再送出
        migrated.close()
