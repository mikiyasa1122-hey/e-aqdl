"""V1 / V14: R1 メモ作成（入力検証・1トランザクション）。"""
from notes import db, service
from notes.errors import ValidationError
from tests.support import BaseCase, count


class CreateTest(BaseCase):
    def create(self, **kw):
        args = {"title": "t", "body": "b", "tags": ()}
        args.update(kw)
        return service.create_note(self.conn, self.u["alice"], **args)

    def test_unit_create_題名の境界(self):
        for title in ("a", "あ" * 100, " a "):
            self.create(title=title)
        for title in ("", " ", "   \n\t", "x" * 101, None, 5):
            with self.assertRaises(ValidationError, msg=repr(title)):
                self.create(title=title)

    def test_unit_create_本文の境界(self):
        self.create(body="")
        self.create(body="x" * 10000)
        with self.assertRaises(ValidationError):
            self.create(body="x" * 10001)
        with self.assertRaises(ValidationError):
            self.create(body=None)

    def test_unit_create_タグの境界(self):
        self.create(tags=[])
        self.create(tags=["a", "b", "c", "d", "e"])
        self.create(tags=["a" * 20, "0", "a-1"])
        bad = [list("abcdef"), ["a", "a", "a", "a", "a", "a"], ["a" * 21], [""], ["A"], ["a_b"],
               ["a b"], ["日本"], ["a\n"], "abc", [1], [None], 5, [True]]
        for tags in bad:
            with self.assertRaises(ValidationError, msg=repr(tags)):
                self.create(tags=tags)

    def test_unit_create_成功時に各1回分が記録される(self):
        nid = self.create(title="題", body="本", tags=["b", "a", "a"], private=True)
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
        self.assertEqual((row["org_id"], row["owner_id"], row["title"], row["body"]), (1, 1, "題", "本"))
        self.assertEqual((row["private"], row["version"], row["views"]), (1, 1, 0))
        tags = [r["tag"] for r in self.conn.execute("SELECT tag FROM note_tags ORDER BY tag")]
        self.assertEqual(tags, ["a", "b"])
        audit = self.conn.execute("SELECT org_id, actor_id, action, detail FROM audit").fetchall()
        self.assertEqual([tuple(r) for r in audit], [(1, 1, "create", f"note={nid}")])

    def test_unit_create_検証エラーでは何も残らない(self):
        with self.assertRaises(ValidationError):
            self.create(tags=["ok", "NG"])
        for t in ("notes", "note_tags", "audit"):
            self.assertEqual(count(self.conn, t), 0)

    def test_unit_create_タグ挿入前の失敗で全て巻き戻る(self):
        self.fail_at("create:tags")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.create(tags=["a"])
        for t in ("notes", "note_tags", "audit"):
            self.assertEqual(count(self.conn, t), 0)
        self.assertFalse(self.conn.in_transaction)
        self.set_hook(lambda s: None)
        nid = self.create()  # 続けて作成できる
        self.assertEqual(count(self.conn, "notes"), 1)
        self.assertEqual(self.conn.execute("SELECT id FROM notes").fetchone()[0], nid)

    def test_unit_create_途中の状態は別接続から見えない(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "n.db")
            conn = db.connect(path)
            users = {"alice": db.add_user(conn, 1, 1, "alice")}
            other = db.connect(path)
            seen = []

            def hook(step):
                seen.append((step, count(other, "notes"), count(other, "audit")))

            self.set_hook(hook)
            service.create_note(conn, users["alice"], "t", "b", ["a"])
            self.assertEqual(seen, [("create:tags", 0, 0)])
            self.assertEqual(count(other, "notes"), 1)
            conn.close()
            other.close()
