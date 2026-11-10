"""R11（アーカイブ）の隠しテスト。依頼の文（request.md）に書いたことだけを確かめる。"""
import unittest

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.alice = db.add_user(self.conn, 1, 10, "alice", api_key="ak-alice-0001")
        self.bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        self.admin = db.add_user(self.conn, 3, 10, "root", role="admin", api_key="ak-root-0003")
        self.eve = db.add_user(self.conn, 9, 99, "eve", api_key="ak-eve-0009")
        self.nid = service.create_note(self.conn, self.alice, "plan", "body text", ["work"])
        self.other = service.create_note(self.conn, self.alice, "plan b", "body text")

    def tearDown(self):
        self.conn.close()

    def audit(self):
        return [tuple(r) for r in self.conn.execute("SELECT actor_id, action, detail FROM audit ORDER BY id")]

    def test_archive_and_unarchive_bump_version(self):
        self.assertEqual(service.archive_note(self.conn, self.alice, self.nid), 2)
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["archived"], n["version"]), (1, 2))
        self.assertEqual(service.unarchive_note(self.conn, self.alice, self.nid), 3)
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["archived"], n["version"]), (0, 3))

    def test_new_note_is_not_archived(self):
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)

    def test_admin_can_archive(self):
        service.archive_note(self.conn, self.admin, self.nid)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 1)

    def test_other_member_forbidden(self):
        with self.assertRaises(Forbidden):
            service.archive_note(self.conn, self.bob, self.nid)
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Forbidden):
            service.unarchive_note(self.conn, self.bob, self.nid)

    def test_other_org_and_missing_not_found(self):
        for user, nid in ((self.eve, self.nid), (self.alice, 999)):
            with self.subTest(user=user["name"]), self.assertRaises(NotFound):
                service.archive_note(self.conn, user, nid)
            with self.subTest(user=user["name"]), self.assertRaises(NotFound):
                service.unarchive_note(self.conn, user, nid)

    def test_private_note_only_owner(self):
        priv = service.create_note(self.conn, self.alice, "p", "b", private=True)
        for user in (self.admin, self.bob):
            with self.subTest(user=user["name"]), self.assertRaises(NotFound):
                service.archive_note(self.conn, user, priv)
        service.archive_note(self.conn, self.alice, priv)

    def test_same_state_conflicts(self):
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["version"], 2)

    def test_audit_recorded(self):
        service.archive_note(self.conn, self.admin, self.nid)
        service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.audit()[-2:], [(3, "archive", f"note={self.nid}"), (1, "unarchive", f"note={self.nid}")])

    def test_failed_archive_leaves_no_audit(self):
        before = self.audit()
        with self.assertRaises(Forbidden):
            service.archive_note(self.conn, self.bob, self.nid)
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.audit(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_list_excludes_archived_by_default(self):
        service.archive_note(self.conn, self.alice, self.nid)
        res = service.list_notes(self.conn, self.alice)
        self.assertEqual(([r["id"] for r in res["items"]], res["total"]), ([self.other], 1))
        res = service.list_notes(self.conn, self.alice, include_archived=True, sort="title")
        self.assertEqual(([r["id"] for r in res["items"]], res["total"]), ([self.nid, self.other], 2))

    def test_search_excludes_archived_by_default(self):
        service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "plan")], [self.other])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "plan", include_archived=True)],
                         [self.nid, self.other])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "plan", ["work"],
                                                                 include_archived=True)], [self.nid])
        self.assertEqual(service.search_notes(self.conn, self.alice, "plan", ["work"]), [])

    def test_include_archived_keeps_visibility_rules(self):
        priv = service.create_note(self.conn, self.alice, "plan secret", "b", private=True)
        service.archive_note(self.conn, self.alice, priv)
        ids = [r["id"] for r in service.list_notes(self.conn, self.bob, include_archived=True)["items"]]
        self.assertNotIn(priv, ids)
        self.assertNotIn(priv, [r["id"] for r in service.search_notes(self.conn, self.bob, "plan", include_archived=True)])
        self.assertEqual(service.list_notes(self.conn, self.eve, include_archived=True)["total"], 0)

    def test_archived_note_cannot_be_updated(self):
        service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 2, body="x")
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["body"], "body text")
        service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.update_note(self.conn, self.alice, self.nid, 3, body="x"), 4)

    def test_archived_note_can_be_deleted_and_transferred(self):
        service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 2)
        self.assertEqual(service.get_note(self.conn, self.bob, self.nid)["archived"], 1)
        service.delete_note(self.conn, self.bob, self.nid)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, self.nid)

    def test_get_note_still_counts_views(self):
        service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.get_note(self.conn, self.bob, self.nid)["views"], 1)
