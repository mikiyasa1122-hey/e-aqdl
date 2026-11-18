"""V4 / V20: R4 更新・削除（権限・楽観的ロック）。"""
import os
import tempfile

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.support import BaseCase, count


class UpdateDeleteTest(BaseCase):
    def make(self, user="alice", **kw):
        return service.create_note(self.conn, self.u[user], "題", "本", **kw)

    def row(self, nid):
        return self.conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()

    def tags(self, nid):
        return [r[0] for r in self.conn.execute("SELECT tag FROM note_tags WHERE note_id=? ORDER BY tag", (nid,))]

    def audit_actions(self):
        return [tuple(r) for r in self.conn.execute("SELECT action, detail FROM audit ORDER BY id")]

    def test_unit_update_作成者と同組織の管理者は更新できる(self):
        nid = self.make(tags=["a"])
        v = service.update_note(self.conn, self.u["alice"], nid, 1, title="新")
        self.assertEqual(v, 2)
        v = service.update_note(self.conn, self.u["admin"], nid, 2, body="新本文")
        self.assertEqual(v, 3)
        r = self.row(nid)
        self.assertEqual((r["title"], r["body"], r["version"]), ("新", "新本文", 3))
        self.assertEqual(self.audit_actions()[1:], [("update", f"note={nid}")] * 2)

    def test_unit_update_権限のない者はForbidden(self):
        nid = self.make()
        for who in ("bob", "carol", "dora"):
            expected = Forbidden if who == "bob" else NotFound
            # 他組織は存在を明かさず NotFound、同組織の別 member は Forbidden
            with self.assertRaises(expected, msg=who):
                service.update_note(self.conn, self.u[who], nid, 1, title="x")
        self.assertEqual(self.row(nid)["version"], 1)
        self.assertEqual(self.row(nid)["title"], "題")

    def test_unit_update_他組織の管理者と同組織の別memberはForbidden系(self):
        nid = self.make()
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.u["bob"], nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.u["bob"], nid)
        # 管理者権限があっても組織が違えば不可（NotFound か Forbidden のどちらでも変更されない）
        for fn in (lambda: service.update_note(self.conn, self.u["dora"], nid, 1, title="x"),
                   lambda: service.delete_note(self.conn, self.u["dora"], nid)):
            with self.assertRaises((Forbidden, NotFound)):
                fn()
        self.assertEqual(self.row(nid)["title"], "題")

    def test_unit_update_他人の非公開は管理者でもNotFound(self):
        nid = self.make(private=True)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["admin"], nid, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.u["admin"], nid)
        self.assertEqual(service.update_note(self.conn, self.u["alice"], nid, 1, title="y"), 2)
        self.assertEqual(count(self.conn, "notes"), 1)

    def test_unit_update_存在しないメモはNotFound(self):
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["alice"], 999, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.u["alice"], 999)

    def test_unit_update_versionが違えばConflictで何も変わらない(self):
        nid = self.make(tags=["a"])
        for v in (0, 2, -1):
            with self.assertRaises(Conflict):
                service.update_note(self.conn, self.u["alice"], nid, v, title="x", tags=["z"])
        r = self.row(nid)
        self.assertEqual((r["title"], r["version"]), ("題", 1))
        self.assertEqual(self.tags(nid), ["a"])
        self.assertEqual(len(self.audit_actions()), 1)

    def test_unit_update_versionの型検査(self):
        nid = self.make()
        for v in (True, "1", None, 1.0):
            with self.assertRaises(ValidationError, msg=repr(v)):
                service.update_note(self.conn, self.u["alice"], nid, v, title="x")

    def test_unit_update_割り込みの更新があればConflictで先の更新が残る(self):
        nid = self.make()
        other = self.conn  # 同じ DB への割り込み（逐次）。別接続のケースは下のファイル DB テスト

        def hook(step):
            if step == "update":
                other.execute(
                    "UPDATE notes SET title = '先の更新', version = version + 1 WHERE id = ?", (nid,))

        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="後の更新")
        r = self.row(nid)
        self.assertEqual((r["title"], r["version"]), ("先の更新", 2))

    def test_unit_update_別接続の割り込みでもConflict(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "n.db")
            c1 = db.connect(path)
            c2 = db.connect(path)
            self.addCleanup(c1.close)
            self.addCleanup(c2.close)
            a = db.add_user(c1, 1, 1, "alice")
            nid = service.create_note(c1, a, "t", "b")

            def hook(step):
                if step == "update":
                    self.set_hook(lambda s: None)
                    service.update_note(c2, a, nid, 1, title="先")

            self.set_hook(hook)
            with self.assertRaises(Conflict):
                service.update_note(c1, a, nid, 1, title="後")
            self.assertEqual(c1.execute("SELECT title, version FROM notes").fetchone()[:], ("先", 2))

    def test_unit_update_割り込みで削除されたらNotFound(self):
        nid = self.make()

        def hook(step):
            if step == "update":
                self.conn.execute("DELETE FROM notes WHERE id = ?", (nid,))

        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u["alice"], nid, 1, title="x")

    def test_unit_update_Noneの項目は変えずtagsは置き換え(self):
        nid = self.make(tags=["a", "b"])
        service.update_note(self.conn, self.u["alice"], nid, 1, title="新")
        r = self.row(nid)
        self.assertEqual((r["title"], r["body"]), ("新", "本"))
        self.assertEqual(self.tags(nid), ["a", "b"])
        service.update_note(self.conn, self.u["alice"], nid, 2, tags=["c", "c"])
        self.assertEqual(self.tags(nid), ["c"])
        service.update_note(self.conn, self.u["alice"], nid, 3, tags=[])
        self.assertEqual(self.tags(nid), [])
        self.assertEqual(self.row(nid)["title"], "新")

    def test_unit_update_更新でも検証され何も変わらない(self):
        nid = self.make(tags=["a"])
        cases = [{"tags": ["OK", "ng"]}, {"tags": ["Bad"]}, {"tags": "abc"}, {"title": ""},
                 {"title": "x" * 101}, {"body": "x" * 10001}, {"title": 5}]
        for kw in cases:
            with self.assertRaises(ValidationError, msg=repr(kw)):
                service.update_note(self.conn, self.u["alice"], nid, 1, **kw)
        r = self.row(nid)
        self.assertEqual((r["title"], r["version"]), ("題", 1))
        self.assertEqual(self.tags(nid), ["a"])

    def test_unit_delete_作成者と管理者は削除できる(self):
        a = self.make(tags=["x", "y"])
        b = self.make(tags=["z"])
        service.delete_note(self.conn, self.u["alice"], a)
        service.delete_note(self.conn, self.u["admin"], b)
        self.assertEqual(count(self.conn, "notes"), 0)
        self.assertEqual(count(self.conn, "note_tags"), 0)
        self.assertEqual(self.audit_actions()[2:], [("delete", f"note={a}"), ("delete", f"note={b}")])

    def test_unit_delete_途中の失敗でメモもタグも残り例外が伝わる(self):
        nid = self.make(tags=["x", "y"])
        self.fail_at("delete:note", RuntimeError("途中失敗"))
        with self.assertRaisesRegex(RuntimeError, "途中失敗"):
            service.delete_note(self.conn, self.u["alice"], nid)
        self.assertEqual(count(self.conn, "notes"), 1)
        self.assertEqual(self.tags(nid), ["x", "y"])
        self.assertEqual(len(self.audit_actions()), 1)
        self.assertFalse(self.conn.in_transaction)

    def test_unit_delete_削除済みのメモの再削除はNotFound(self):
        nid = self.make()
        service.delete_note(self.conn, self.u["alice"], nid)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.u["alice"], nid)
