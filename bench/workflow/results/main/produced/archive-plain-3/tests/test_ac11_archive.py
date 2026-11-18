"""R11: アーカイブ・復元。"""
from notes import service
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
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["archived"], n["version"]), (1, 2))
        self.assertEqual(service.unarchive_note(self.conn, self.admin, self.nid), 3)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)
        self.assertEqual(self.audit()[-2:], [("archive", f"note={self.nid}"), ("unarchive", f"note={self.nid}")])

    def test_state_conflicts(self):
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.count("audit"), 2)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["version"], 2)

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
        self.assertEqual(self.count("audit"), 2)

    def test_list_and_search_hide_archived(self):
        other = service.create_note(self.conn, self.alice, "other", "findme")
        service.archive_note(self.conn, self.alice, self.nid)
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual((r["total"], [i["id"] for i in r["items"]]), (1, [other]))
        r = service.list_notes(self.conn, self.alice, include_archived=True)
        self.assertEqual(r["total"], 2)
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "findme")], [other])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "findme", include_archived=True)), 2)

    def test_update_conflicts_but_delete_and_transfer_work(self):
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 2, body="x")
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 1)
        service.delete_note(self.conn, self.bob, self.nid)
        self.assertEqual(self.count("notes"), 0)

    def test_failure_leaves_nothing(self):
        self.conn.execute("DROP TABLE audit")
        with self.assertRaises(Exception):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)
