"""R14・AC15・V20・V21・V25: get・update・delete・transfer とアーカイブ。"""
from notes import service
from notes.errors import Conflict

from .base import Base


class ArchiveReadWriteTest(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["x"])

    def row(self):
        return dict(self.conn.execute("SELECT * FROM notes WHERE id = ?", (self.nid,)).fetchone())

    def test_get_shows_archived_and_counts_views(self):
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)
        service.archive_note(self.conn, self.alice, self.nid)
        n = service.get_note(self.conn, self.bob, self.nid)
        self.assertEqual((n["archived"], n["views"], n["tags"]), (1, 2, ["x"]))
        service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["archived"], 0)

    def test_update_archived_conflicts_for_current_and_stale_version(self):
        service.archive_note(self.conn, self.alice, self.nid)  # version 2
        before = self.row()
        for ver in (2, 1):
            with self.subTest(version=ver):
                with self.assertRaises(Conflict):
                    service.update_note(self.conn, self.alice, self.nid, ver, title="new", tags=["z"])
                self.assertEqual(self.row(), before)
                self.assertEqual(self.conn.execute("SELECT tag FROM note_tags").fetchall()[0]["tag"], "x")
                self.assertFalse(self.conn.in_transaction)
        service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.update_note(self.conn, self.alice, self.nid, 3, title="new"), 4)

    def test_update_archived_write_guard_blocks_stale_check(self):
        """判定の後に別接続でアーカイブされても、書き込みの条件で更新が通らない。"""
        def archive_inside(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                self.conn.execute("UPDATE notes SET archived = 1, version = version + 1 WHERE id = ?", (self.nid,))
        service.hooks.before_write = archive_inside
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="new")
        self.assertEqual(self.row()["title"], "t")

    def test_delete_archived(self):
        service.archive_note(self.conn, self.alice, self.nid)
        service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.count("notes"), 0)

    def test_transfer_keeps_archived_and_bumps_version(self):
        service.archive_note(self.conn, self.alice, self.nid)  # version 2
        updated = self.row()["updated_at"]
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 1)
        r = self.row()
        self.assertEqual((r["owner_id"], r["version"], r["archived"], r["updated_at"]), (self.bob["id"], 3, 1, updated))
        actions = [a["action"] for a in service.export_audit(self.conn, self.admin)]
        self.assertEqual(actions, ["create", "archive", "transfer"])
