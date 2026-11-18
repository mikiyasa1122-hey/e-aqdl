"""R11: メモのアーカイブ・復元。"""
from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound

from .base import Base


class ArchiveTest(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "findme", "b", ["a"])

    def audit(self):
        return [(a["action"], a["detail"]) for a in service.export_audit(self.conn, self.admin)]

    def test_archive_and_unarchive(self):
        self.assertEqual(service.archive_note(self.conn, self.alice, self.nid), 2)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 1)
        self.assertEqual(service.unarchive_note(self.conn, self.admin, self.nid), 3)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)
        self.assertEqual(self.audit()[-2:], [("archive", f"note={self.nid}"), ("unarchive", f"note={self.nid}")])

    def test_conflict_when_already_in_state(self):
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["version"], 2)
        self.assertEqual(self.count("audit"), 2)

    def test_permissions(self):
        with self.assertRaises(Forbidden):
            service.archive_note(self.conn, self.bob, self.nid)
        for u in (self.eve, self.eve_admin):
            with self.assertRaises(NotFound):
                service.archive_note(self.conn, u, self.nid)
        with self.assertRaises(NotFound):
            service.archive_note(self.conn, self.alice, 999)
        priv = service.create_note(self.conn, self.alice, "p", "b", private=True)
        with self.assertRaises(NotFound):
            service.archive_note(self.conn, self.admin, priv)
        self.assertEqual(service.archive_note(self.conn, self.alice, priv), 2)

    def test_list_and_search_hide_archived(self):
        other = service.create_note(self.conn, self.alice, "findme2", "b")
        service.archive_note(self.conn, self.alice, self.nid)
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual((r["total"], [i["id"] for i in r["items"]]), (1, [other]))
        r = service.list_notes(self.conn, self.alice, include_archived=True)
        self.assertEqual(r["total"], 2)
        self.assertEqual([n["id"] for n in service.search_notes(self.conn, self.alice, "findme")], [other])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "findme", include_archived=True)), 2)

    def test_update_conflicts_but_delete_and_transfer_work(self):
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 2, body="x")
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 1)
        service.delete_note(self.conn, self.bob, self.nid)
        self.assertEqual(self.count("notes"), 0)

    def test_failure_leaves_nothing(self):
        self.conn.execute("CREATE TRIGGER t BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'x'); END")
        with self.assertRaises(Exception):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["version"], 1)

    def test_old_db_is_migrated(self):
        import os, sqlite3, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript(db.SCHEMA.replace("    archived INTEGER NOT NULL DEFAULT 0,\n", ""))
        c.close()
        conn = db.connect(path)
        self.assertIn("archived", [r["name"] for r in conn.execute("PRAGMA table_info(notes)")])
        conn.close()
