"""R11・AC11・AC12・AC18: アーカイブ・復元の権限・状態・version・updated_at。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound

from .base import Base


class ArchiveStateTest(Base):
    def row(self, nid):
        return dict(self.conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone())

    def test_permission_table_archive_and_unarchive(self):
        """作成者・管理者は成功し、戻り値が保存値と一致する。"""
        for actor_name in ("alice", "admin"):
            actor = getattr(self, actor_name)
            with self.subTest(actor=actor_name):
                nid = service.create_note(self.conn, self.alice, "t", "b")
                before = self.row(nid)["version"]
                self.assertEqual(service.archive_note(self.conn, actor, nid), before + 1)
                r = self.row(nid)
                self.assertEqual((r["version"], r["archived"]), (before + 1, 1))
                self.assertEqual(service.unarchive_note(self.conn, actor, nid), before + 2)
                r = self.row(nid)
                self.assertEqual((r["version"], r["archived"]), (before + 2, 0))

    def test_denied_cases_leave_state_unchanged(self):
        pub = service.create_note(self.conn, self.alice, "pub", "b")
        priv = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        service.archive_note(self.conn, self.alice, pub)  # unarchive の拒否検証用に1件アーカイブ済みにする
        pub2 = service.create_note(self.conn, self.alice, "pub2", "b")
        cases = [
            (NotFound, self.eve, pub2), (NotFound, self.eve_admin, pub2),  # 別組織
            (NotFound, self.admin, priv), (NotFound, self.bob, priv),  # 非公開は管理者でも
            (NotFound, self.alice, 99999),  # 存在しない
            (Forbidden, self.bob, pub2),  # 同組織の一般利用者
        ]
        for exc, actor, nid in cases:
            for fn in (service.archive_note, service.unarchive_note):
                with self.subTest(exc=exc.__name__, actor=actor["name"], nid=nid, fn=fn.__name__):
                    snap = (self.count("notes"), self.count("audit"), self.row(nid) if nid != 99999 else None)
                    with self.assertRaises(exc):
                        fn(self.conn, actor, nid)
                    self.assertEqual(snap, (self.count("notes"), self.count("audit"),
                                            self.row(nid) if nid != 99999 else None))
        # 復元の拒否（アーカイブ済みに対しても権限が先）
        with self.assertRaises(Forbidden):
            service.unarchive_note(self.conn, self.bob, pub)
        self.assertEqual(self.row(pub)["archived"], 1)

    def test_conflict_when_already_in_state(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(Conflict):
            service.unarchive_note(self.conn, self.alice, nid)
        self.assertEqual((self.row(nid)["version"], self.row(nid)["archived"]), (1, 0))
        service.archive_note(self.conn, self.alice, nid)
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, nid)
        self.assertEqual((self.row(nid)["version"], self.row(nid)["archived"]), (2, 1))
        self.assertFalse(self.conn.in_transaction)

    def test_round_trip_adds_two_versions(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.archive_note(self.conn, self.alice, nid)
        service.unarchive_note(self.conn, self.alice, nid)
        r = self.row(nid)
        self.assertEqual((r["version"], r["archived"]), (3, 0))

    def test_updated_at_and_list_order_unchanged(self):
        """updated_at を更新する実装にすると失敗する（AC18）。"""
        ids = []
        for i, ts in enumerate((1000, 3000, 2000)):
            nid = service.create_note(self.conn, self.alice, f"n{i}", "b")
            self.conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (ts, nid))
            ids.append(nid)
        order = lambda: [i["id"] for i in service.list_notes(self.conn, self.alice, include_archived=True)["items"]]
        before = order()
        self.assertEqual(before, [ids[1], ids[2], ids[0]])
        service.archive_note(self.conn, self.alice, ids[0])
        self.assertEqual(self.row(ids[0])["updated_at"], 1000)
        self.assertEqual(order(), before)
        service.unarchive_note(self.conn, self.alice, ids[0])
        self.assertEqual(self.row(ids[0])["updated_at"], 1000)
        self.assertEqual(order(), before)
