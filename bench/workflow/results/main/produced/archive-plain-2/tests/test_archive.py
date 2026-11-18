"""R11: アーカイブ・復元。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound

from .base import Base


class ArchiveTest(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "findme", "b", ["a"])

    def test_archive_and_unarchive(self):
        self.assertEqual(service.archive_note(self.conn, self.alice, self.nid), 2)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 1)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.unarchive_note(self.conn, self.admin, self.nid), 3)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        actions = [(a["action"], a["detail"]) for a in service.export_audit(self.conn, self.admin)]
        self.assertEqual(actions[-2:], [("archive", f"note={self.nid}"), ("unarchive", f"note={self.nid}")])

    def test_permissions(self):
        with self.assertRaises(Forbidden):
            service.archive_note(self.conn, self.bob, self.nid)
        for u in (self.eve, self.eve_admin):
            with self.assertRaises(NotFound):
                service.archive_note(self.conn, u, self.nid)
        priv = service.create_note(self.conn, self.alice, "p", "b", private=True)
        with self.assertRaises(NotFound):
            service.archive_note(self.conn, self.admin, priv)
        with self.assertRaises(NotFound):
            service.archive_note(self.conn, self.alice, 999)
        self.assertEqual(self.count("audit"), 2)

    def test_list_and_search_exclude_archived(self):
        other = service.create_note(self.conn, self.alice, "findme2", "b")
        service.archive_note(self.conn, self.alice, self.nid)
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual((r["total"], [i["id"] for i in r["items"]]), (1, [other]))
        self.assertEqual(service.list_notes(self.conn, self.alice, include_archived=True)["total"], 2)
        self.assertEqual([n["id"] for n in service.search_notes(self.conn, self.alice, "findme")], [other])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "findme", include_archived=True)), 2)

    def test_update_blocked_but_delete_and_transfer_ok(self):
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 2, body="x")
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 1)
        service.delete_note(self.conn, self.bob, self.nid)
        self.assertEqual(self.count("notes"), 0)

    def test_failure_leaves_nothing(self):
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["version"], n["archived"], self.count("audit")), (1, 0, 1))
