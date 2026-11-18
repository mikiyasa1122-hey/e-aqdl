"""AC11〜AC13（単体）: archive_note / unarchive_note の動作・認可・二重操作。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound

from .base import Base


class ArchiveBase(Base):
    def snap(self, nid):
        r = service._row(self.conn, nid)
        return (r["version"], r["archived"], self.count("audit"))


class ArchiveBasicTest(ArchiveBase):
    def test_v1_owner_archive_and_unarchive(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual(service.archive_note(self.conn, self.alice, nid), 2)
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["archived"], n["version"]), (1, 2))
        self.assertEqual(service.unarchive_note(self.conn, self.alice, nid), 3)
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["archived"], n["version"]), (0, 3))

    def test_v1_admin_can_archive(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual(service.archive_note(self.conn, self.admin, nid), 2)
        self.assertEqual(service.unarchive_note(self.conn, self.admin, nid), 3)

    def test_v1_updated_at_unchanged(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        before = service._row(self.conn, nid)["updated_at"]
        service.archive_note(self.conn, self.alice, nid)
        self.assertEqual(service._row(self.conn, nid)["updated_at"], before)

    def test_v2_denied_cases_leave_state_unchanged(self):
        pub = service.create_note(self.conn, self.alice, "t", "b")
        priv = service.create_note(self.conn, self.alice, "p", "b", private=True)
        cases = [
            (Forbidden, self.bob, pub),
            (NotFound, self.eve, pub),
            (NotFound, self.eve_admin, pub),
            (NotFound, self.admin, priv),  # 非公開は管理者でも NotFound
            (NotFound, self.bob, priv),
            (NotFound, self.alice, 9999),
        ]
        for exc, user, nid in cases:
            for fn in (service.archive_note, service.unarchive_note):
                with self.subTest(exc=exc.__name__, user=user["name"], nid=nid, fn=fn.__name__):
                    before = self.snap(nid) if nid != 9999 else self.count("audit")
                    with self.assertRaises(exc):
                        fn(self.conn, user, nid)
                    after = self.snap(nid) if nid != 9999 else self.count("audit")
                    self.assertEqual(before, after)

    def test_v2_denied_on_archived_note_also_unchanged(self):
        pub = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, pub)
        before = self.snap(pub)
        with self.assertRaises(Forbidden):
            service.unarchive_note(self.conn, self.bob, pub)
        with self.assertRaises(NotFound):
            service.unarchive_note(self.conn, self.eve_admin, pub)
        self.assertEqual(self.snap(pub), before)

    def test_v3_forbidden_for_non_owner_member_and_notfound_for_other_org_admin(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(Forbidden):
            service.archive_note(self.conn, self.bob, nid)
        with self.assertRaises(NotFound):
            service.archive_note(self.conn, self.eve_admin, nid)
        # 同じ組織の管理者・作成者は通る（認可条件が緩すぎ・厳しすぎの両方を検出）
        service.archive_note(self.conn, self.admin, nid)
        service.unarchive_note(self.conn, self.alice, nid)

    def test_v4_double_archive_and_unarchive_conflict(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        before = self.snap(nid)
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, nid)
        self.assertEqual(self.snap(nid), before)
        service.archive_note(self.conn, self.alice, nid)
        before = self.snap(nid)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, nid)
        self.assertEqual(self.snap(nid), before)
        self.assertFalse(self.conn.in_transaction)

    def test_audit_entries(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        base = self.count("audit")
        service.archive_note(self.conn, self.alice, nid)
        service.unarchive_note(self.conn, self.admin, nid)
        rows = [(r["action"], r["detail"], r["actor_id"]) for r in
                self.conn.execute("SELECT * FROM audit ORDER BY id").fetchall()[base:]]
        self.assertEqual(rows, [("archive", f"note={nid}", 1), ("unarchive", f"note={nid}", 3)])


class ArchivedUpdateTest(ArchiveBase):
    """V12: アーカイブ済みは更新不可、削除・移管は可。"""

    def test_update_conflict_even_with_latest_version(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        v = service.archive_note(self.conn, self.alice, nid)
        before = self.snap(nid)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, v, body="new")
        self.assertEqual(self.snap(nid), before)
        self.assertEqual(service._row(self.conn, nid)["body"], "b")
        v = service.unarchive_note(self.conn, self.alice, nid)
        self.assertEqual(service.update_note(self.conn, self.alice, nid, v, body="new"), v + 1)

    def test_update_denied_user_gets_authz_error_first(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        v = service.archive_note(self.conn, self.alice, nid)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, v, body="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.eve_admin, nid, v, body="x")

    def test_delete_and_transfer_succeed(self):
        n1 = service.create_note(self.conn, self.alice, "t", "b")
        n2 = service.create_note(self.conn, self.alice, "t2", "b")
        service.archive_note(self.conn, self.alice, n1)
        service.archive_note(self.conn, self.alice, n2)
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 2)
        r = service._row(self.conn, n1)
        self.assertEqual((r["owner_id"], r["version"], r["archived"]), (2, 3, 1))
        service.delete_note(self.conn, self.bob, n2)
        self.assertIsNone(service._row(self.conn, n2))
