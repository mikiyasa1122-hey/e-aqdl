"""AC20・K14: archived 列（新規DB・旧スキーマDBの移行・冪等）。テストは一時ファイルのDBだけを使う。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
INSERT INTO users VALUES (1, 10, 'alice', 'member', 'ak-alice-0001');
INSERT INTO notes (id, org_id, owner_id, title, body, private, version, views, updated_at)
    VALUES (1, 10, 1, 'old', 'body', 0, 3, 5, 111), (2, 10, 1, 'old2', 'body2', 1, 1, 0, 222);
INSERT INTO note_tags VALUES (1, 'x');
"""


def archived_column(conn):
    return [dict(r) for r in conn.execute("PRAGMA table_info(notes)") if r["name"] == "archived"]


class NewDbTest(unittest.TestCase):
    def test_new_db_has_archived_column(self):
        conn = db.connect()
        cols = archived_column(conn)
        self.assertEqual(len(cols), 1)
        self.assertEqual((cols[0]["type"], cols[0]["notnull"], cols[0]["dflt_value"]), ("INTEGER", 1, "0"))

    def test_create_note_defaults_to_zero(self):
        conn = db.connect()
        u = db.add_user(conn, 1, 10, "a")
        nid = service.create_note(conn, u, "t", "b")
        self.assertEqual(conn.execute("SELECT archived FROM notes WHERE id = ?", (nid,)).fetchone()[0], 0)


class OldSchemaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "old.db")
        raw = sqlite3.connect(self.path)
        raw.executescript(OLD_SCHEMA)
        raw.commit()
        raw.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_old_db_is_migrated_and_rows_kept(self):
        conn = db.connect(self.path)
        try:
            self.assertEqual(len(archived_column(conn)), 1)
            rows = [dict(r) for r in conn.execute("SELECT * FROM notes ORDER BY id")]
            self.assertEqual([(r["id"], r["title"], r["version"], r["views"], r["updated_at"], r["archived"]) for r in rows],
                             [(1, "old", 3, 5, 111, 0), (2, "old2", 1, 0, 222, 0)])
            alice = db.get_user(conn, 1)
            n = service.get_note(conn, alice, 1)
            self.assertEqual((n["archived"], n["tags"], n["views"]), (0, ["x"], 6))
            self.assertEqual(service.archive_note(conn, alice, 1), 4)
            self.assertEqual([i["id"] for i in service.list_notes(conn, alice)["items"]], [2])
        finally:
            conn.close()

    def test_migration_is_idempotent(self):
        for _ in range(3):
            conn = db.connect(self.path)
            conn.close()
        conn = db.connect(self.path)
        conn2 = db.connect(self.path)  # 連続して2つ開いても失敗しない
        try:
            self.assertEqual(len(archived_column(conn)), 1)
            self.assertEqual(len(archived_column(conn2)), 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2)
        finally:
            conn.close()
            conn2.close()

    def test_archived_state_survives_reconnect(self):
        conn = db.connect(self.path)
        service.archive_note(conn, db.get_user(conn, 1), 1)
        conn.close()
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT archived, version FROM notes WHERE id = 1").fetchone()[:], (1, 4))
        finally:
            conn.close()

    def test_unrelated_operational_error_is_not_hidden(self):
        # 列が足せない失敗（列が現れないまま）は隠さず投げる
        class FakeConn:
            def execute(self, sql, *a):
                if sql.startswith("ALTER"):
                    raise sqlite3.OperationalError("database is locked")
                return [{"name": "id"}]
        with self.assertRaises(sqlite3.OperationalError):
            db._migrate(FakeConn())

    def test_duplicate_column_race_is_success(self):
        # 別の接続が先に列を足した（ALTER が duplicate column で失敗し、列が見える）場合は成功とみなす
        calls = []

        class FakeConn:
            def execute(self, sql, *a):
                if sql.startswith("ALTER"):
                    calls.append(sql)
                    raise sqlite3.OperationalError("duplicate column name: archived")
                return [{"name": "id"}] if not calls else [{"name": "id"}, {"name": "archived"}]
        db._migrate(FakeConn())
        self.assertEqual(len(calls), 1)
