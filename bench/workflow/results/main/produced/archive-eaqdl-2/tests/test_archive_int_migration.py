"""R15・AC16・V22・V23: 旧スキーマ DB の移行。"""
import os
import sqlite3
import tempfile
import unittest

from notes import db, service

OLD_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('member', 'admin')), api_key TEXT NOT NULL);
CREATE TABLE notes (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL, body TEXT NOT NULL, private INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
    views INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
CREATE TABLE note_tags (note_id INTEGER NOT NULL REFERENCES notes(id), tag TEXT NOT NULL, PRIMARY KEY (note_id, tag));
CREATE TABLE audit (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL);
"""


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "old.db")
        raw = sqlite3.connect(self.path)
        raw.executescript(OLD_SCHEMA)
        raw.execute("INSERT INTO users VALUES (1, 10, 'alice', 'member', 'ak-alice-0001')")
        raw.execute("INSERT INTO notes VALUES (1, 10, 1, 't1', 'b1', 0, 3, 5, 111)")
        raw.execute("INSERT INTO notes VALUES (2, 10, 1, 't2', 'b2', 1, 1, 0, 222)")
        raw.execute("INSERT INTO note_tags VALUES (1, 'x')")
        raw.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (10, 1, 'create', 'note=1')")
        raw.commit()
        raw.close()

    def snapshot(self, conn):
        return ([tuple(r) for r in conn.execute("SELECT id, org_id, owner_id, title, body, private, version, views, updated_at FROM notes")],
                [tuple(r) for r in conn.execute("SELECT * FROM note_tags")],
                [tuple(r) for r in conn.execute("SELECT * FROM audit")])

    def test_old_db_migrated_and_repeatable(self):
        for i in range(3):
            conn = db.connect(self.path)
            with self.subTest(i=i):
                self.assertEqual([r["archived"] for r in conn.execute("SELECT archived FROM notes ORDER BY id")], [0, 0])
                snap = self.snapshot(conn)
                self.assertEqual(snap[0][0], (1, 10, 1, "t1", "b1", 0, 3, 5, 111))
                self.assertEqual(snap[1], [(1, "x")])
                self.assertEqual(snap[2], [(1, 10, 1, "create", "note=1")])
                self.assertFalse(conn.in_transaction)
            conn.close()
        conn = db.connect(self.path)
        alice = db.get_user(conn, 1)
        self.assertEqual(service.archive_note(conn, alice, 1), 4)
        self.assertEqual([i["id"] for i in service.list_notes(conn, alice)["items"]], [2])
        conn.close()
        conn = db.connect(self.path)  # アーカイブ済みの値は再接続で消えない
        self.assertEqual(conn.execute("SELECT archived FROM notes WHERE id = 1").fetchone()[0], 1)
        conn.close()

    def test_new_db_column_definition(self):
        conn = db.connect()
        col = [r for r in conn.execute("PRAGMA table_info(notes)") if r["name"] == "archived"]
        self.assertEqual(len(col), 1)
        self.assertEqual((col[0]["notnull"], col[0]["dflt_value"], col[0]["type"]), (1, "0", "INTEGER"))

    def test_migrated_column_definition_matches_new(self):
        conn = db.connect(self.path)
        col = next(r for r in conn.execute("PRAGMA table_info(notes)") if r["name"] == "archived")
        self.assertEqual((col["notnull"], col["dflt_value"]), (1, "0"))
        conn.close()
