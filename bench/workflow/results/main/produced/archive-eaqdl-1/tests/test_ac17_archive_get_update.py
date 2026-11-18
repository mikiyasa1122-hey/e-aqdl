"""AC17〜AC19・K13: アーカイブ済みメモの get・update・delete・transfer。"""
from notes import db, service
from notes.errors import Conflict

from .base import Base
from .filedb import FileDbBase


class GetTest(Base):
    """V10"""

    def test_get_archived_and_active(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["a", "b"])
        n = service.get_note(self.conn, self.bob, nid)
        self.assertEqual((n["archived"], n["views"]), (0, 1))
        service.archive_note(self.conn, self.alice, nid)
        n = service.get_note(self.conn, self.bob, nid)
        self.assertEqual((n["archived"], n["views"], n["tags"], n["title"]), (1, 2, ["a", "b"], "t"))
        self.assertIsInstance(n["archived"], int)
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(n["views"], 3)


class ArchivedUpdateTest(Base):
    """V11（単体の範囲）: アーカイブ済みの update は Conflict で何も変わらない。"""

    def test_update_archived_is_conflict_for_latest_and_stale_version(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        service.archive_note(self.conn, self.alice, nid)  # version 2
        audits = self.count("audit")
        for version in (2, 1, 99):
            for user in (self.alice, self.admin):
                with self.subTest(version=version, user=user["name"]), self.assertRaises(Conflict):
                    service.update_note(self.conn, user, nid, version, title="new", body="new", tags=["z"])
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("t", "b", ["a"], 2))
        self.assertEqual(self.count("audit"), audits)
        self.assertFalse(self.conn.in_transaction)

    def test_update_after_restore_works(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, nid)
        service.unarchive_note(self.conn, self.alice, nid)
        self.assertEqual(service.update_note(self.conn, self.alice, nid, 3, body="c"), 4)

    def test_archived_conflict_is_decided_before_validation(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, nid)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 2, tags=["BAD"])


class DeleteTransferTest(Base):
    """V14"""

    def test_delete_archived(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        service.archive_note(self.conn, self.alice, nid)
        service.delete_note(self.conn, self.alice, nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (0, 0))

    def test_transfer_includes_archived(self):
        a = service.create_note(self.conn, self.alice, "a", "b")
        b = service.create_note(self.conn, self.alice, "b", "b")
        service.archive_note(self.conn, self.alice, a)  # version 2
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 2)
        rows = {r["id"]: r for r in self.conn.execute("SELECT * FROM notes")}
        self.assertEqual((rows[a]["owner_id"], rows[a]["version"], rows[a]["archived"]), (self.bob["id"], 3, 1))
        self.assertEqual((rows[b]["owner_id"], rows[b]["version"], rows[b]["archived"]), (self.bob["id"], 2, 0))
        last = self.conn.execute("SELECT detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(last, f"from={self.alice['id']} to={self.bob['id']} count=2")

    def test_transfer_only_archived_notes(self):
        a = service.create_note(self.conn, self.alice, "a", "b")
        service.archive_note(self.conn, self.alice, a)
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 1)
        # 新しい持ち主が復元・更新系を扱える
        service.unarchive_note(self.conn, self.bob, a)


class IntegrationTest(FileDbBase):
    """V11・V13: ファイルDB・別接続。"""

    def test_update_conflict_when_archived_by_other_connection_during_update(self):
        other = db.connect(self.path)

        def hook(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.archive_note(other, self.alice, self.nid)
        service.hooks.before_write = hook
        try:
            with self.assertRaises(Conflict):
                service.update_note(self.conn, self.alice, self.nid, 1, title="x", tags=["q"])
        finally:
            other.close()
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["title"], n["tags"], n["version"], n["archived"]), ("t", ["a"], 2, 1))
        self.assertEqual(self.audits("update"), [])

    def test_delete_archived_removes_tags_and_audits(self):
        service.archive_note(self.conn, self.alice, self.nid)
        service.delete_note(self.conn, self.alice, self.nid)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM notes WHERE id = ?", (self.nid,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        self.assertEqual(len(self.audits("delete")), 1)

    def test_delete_archived_failure_leaves_everything(self):
        service.archive_note(self.conn, self.alice, self.nid)
        audits = self.audits()
        self.fail_at("delete:note")
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.state(), (2, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 1)
        self.assertEqual(self.audits(), audits)

    def test_transfer_failure_keeps_archived_state_and_versions(self):
        n2 = service.create_note(self.conn, self.alice, "n2", "b")
        service.archive_note(self.conn, self.alice, self.nid)
        bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        self.fail_at(f"transfer:{n2}")
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, bob)
        rows = {r["id"]: (r["owner_id"], r["version"], r["archived"]) for r in self.conn.execute("SELECT * FROM notes")}
        self.assertEqual(rows, {self.nid: (1, 2, 1), n2: (1, 1, 0)})
