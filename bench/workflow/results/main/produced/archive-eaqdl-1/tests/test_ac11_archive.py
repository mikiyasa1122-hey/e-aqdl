"""AC11〜AC13・K9: archive_note・unarchive_note の version・権限表・Conflict。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound

from .base import Base


class ArchiveBase(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        self.priv = service.create_note(self.conn, self.alice, "p", "b", private=True)

    def state(self, nid):
        row = self.conn.execute("SELECT version, archived FROM notes WHERE id = ?", (nid,)).fetchone()
        return (row["version"], row["archived"])

    def audit_count(self, action):
        return self.conn.execute("SELECT COUNT(*) FROM audit WHERE action = ?", (action,)).fetchone()[0]


class ArchiveVersionTest(ArchiveBase):
    """V1: 作成者・管理者の archive→unarchive。"""

    def test_owner_and_admin_archive_and_restore(self):
        for user in (self.alice, self.admin):
            with self.subTest(user=user["name"]):
                nid = service.create_note(self.conn, self.alice, "x", "y")
                self.assertEqual(service.archive_note(self.conn, user, nid), 2)
                n = service.get_note(self.conn, self.alice, nid)
                self.assertEqual((n["archived"], n["version"]), (1, 2))
                self.assertEqual(service.unarchive_note(self.conn, user, nid), 3)
                n = service.get_note(self.conn, self.alice, nid)
                self.assertEqual((n["archived"], n["version"]), (0, 3))

    def test_updated_at_and_content_are_untouched(self):
        before = dict(self.conn.execute("SELECT * FROM notes WHERE id = ?", (self.nid,)).fetchone())
        service.archive_note(self.conn, self.alice, self.nid)
        after = dict(self.conn.execute("SELECT * FROM notes WHERE id = ?", (self.nid,)).fetchone())
        for k in ("title", "body", "private", "owner_id", "updated_at"):
            self.assertEqual(before[k], after[k])
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["tags"], ["a"])


class PermissionTableTest(ArchiveBase):
    """V2・V3: 権限表を archive・unarchive・update で総当たりし、例外型を比べる。"""

    def cases(self):
        # (名前, 利用者, メモ id, 期待する例外)
        return [
            ("作成者", self.alice, self.nid, None),
            ("同組織管理者", self.admin, self.nid, None),
            ("同組織 member", self.bob, self.nid, Forbidden),
            ("他組織 member", self.eve, self.nid, NotFound),
            ("他組織管理者", self.eve_admin, self.nid, NotFound),
            ("非公開の他人 member", self.bob, self.priv, NotFound),
            ("管理者 vs 非公開", self.admin, self.priv, NotFound),
            ("存在しない", self.alice, 9999, NotFound),
        ]

    def test_archive_table(self):
        for name, user, nid, exc in self.cases():
            with self.subTest(name=name):
                if exc is None:
                    service.archive_note(self.conn, user, nid)
                    service.unarchive_note(self.conn, user, nid)  # 次の行のために戻す
                    continue
                before = (self.state(nid), self.count("audit")) if nid != 9999 else (None, self.count("audit"))
                with self.assertRaises(exc):
                    service.archive_note(self.conn, user, nid)
                after = (self.state(nid), self.count("audit")) if nid != 9999 else (None, self.count("audit"))
                self.assertEqual(before, after)

    def test_unarchive_table(self):
        service.archive_note(self.conn, self.alice, self.nid)
        service.archive_note(self.conn, self.alice, self.priv)
        for name, user, nid, exc in self.cases():
            with self.subTest(name=name):
                if exc is None:
                    service.unarchive_note(self.conn, user, nid)
                    service.archive_note(self.conn, self.alice, nid)
                    continue
                before = (self.state(nid), self.count("audit")) if nid != 9999 else (None, self.count("audit"))
                with self.assertRaises(exc):
                    service.unarchive_note(self.conn, user, nid)
                after = (self.state(nid), self.count("audit")) if nid != 9999 else (None, self.count("audit"))
                self.assertEqual(before, after)
                if nid != 9999:
                    self.assertEqual(self.state(nid)[1], 1)

    def test_same_exception_types_as_update(self):
        service.archive_note(self.conn, self.alice, self.priv)  # アーカイブ済みでも見えない人は NotFound
        service.unarchive_note(self.conn, self.alice, self.priv)
        for name, user, nid, exc in self.cases():
            if exc is None:
                continue
            with self.subTest(name=name):
                calls = (lambda c, u, n: service.update_note(c, u, n, 1, body="x"),
                         service.archive_note, service.unarchive_note)
                for call in calls:
                    with self.assertRaises(exc) as cm:
                        call(self.conn, user, nid)
                    self.assertIs(type(cm.exception), exc)

    def test_archived_note_still_hidden_from_outsiders(self):
        service.archive_note(self.conn, self.alice, self.nid)
        for user in (self.eve, self.eve_admin):
            with self.assertRaises(NotFound):
                service.unarchive_note(self.conn, user, self.nid)
            with self.assertRaises(NotFound):
                service.archive_note(self.conn, user, self.nid)  # Conflict で存在を明かさない
        with self.assertRaises(Forbidden):
            service.unarchive_note(self.conn, self.bob, self.nid)


class ConflictTest(ArchiveBase):
    """V4: すでにその状態なら Conflict で、version・監査ログが不変。"""

    def test_double_archive_is_conflict(self):
        service.archive_note(self.conn, self.alice, self.nid)
        audits = self.count("audit")
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.admin, self.nid)
        self.assertEqual(self.state(self.nid), (2, 1))
        self.assertEqual(self.count("audit"), audits)
        self.assertEqual(self.audit_count("archive"), 1)

    def test_unarchive_of_active_note_is_conflict(self):
        audits = self.count("audit")
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.state(self.nid), (1, 0))
        self.assertEqual(self.count("audit"), audits)

    def test_unarchive_twice_is_conflict(self):
        service.archive_note(self.conn, self.alice, self.nid)
        service.unarchive_note(self.conn, self.alice, self.nid)
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.state(self.nid), (3, 0))
        self.assertFalse(self.conn.in_transaction)
