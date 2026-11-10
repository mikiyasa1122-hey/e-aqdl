from notes import service
from notes.errors import ValidationError
from tests.helpers import DbTestCase, snapshot


class CreateTest(DbTestCase):
    def test_create_境界値で成功する(self):
        nid = self.note(title="あ" * 100, body="x" * 10000, tags=("a-1", "z" * 20, "b", "c", "d"))
        row = self.conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["views"], 0)
        self.assertEqual(row["owner_id"], 1)
        self.assertEqual(row["org_id"], 1)
        self.assertEqual(row["private"], 0)

    def test_create_空本文と空タグは許す(self):
        nid = self.note(body="", tags=())
        self.assertEqual(service.get_note(self.conn, self.u["alice"], nid)["tags"], [])

    def test_create_題名の不正を拒否し何も残さない(self):
        for title in ("", "   ", "\t\n", "x" * 101, None, 5):
            before = snapshot(self.conn)
            with self.assertRaises(ValidationError):
                service.create_note(self.conn, self.u["alice"], title, "本文")
            self.assertEqual(snapshot(self.conn), before)

    def test_create_本文の不正を拒否(self):
        for body in ("x" * 10001, None, 3):
            with self.assertRaises(ValidationError):
                service.create_note(self.conn, self.u["alice"], "t", body)
        self.assertEqual(snapshot(self.conn), ((), (), ()))

    def test_create_タグの不正を拒否(self):
        bad = ["abc", ("A",), ("a_b",), ("",), ("x" * 21,), ("a", "b", "c", "d", "e", "f"),
               ("a", "a", "a", "a", "a", "a"), (1,), None, ("日本",), ("a b",)]
        for tags in bad:
            with self.assertRaises(ValidationError, msg=repr(tags)):
                service.create_note(self.conn, self.u["alice"], "t", "b", tags)
        self.assertEqual(snapshot(self.conn), ((), (), ()))

    def test_create_タグの重複は保存時に除かれ上限は重複込みで数える(self):
        nid = self.note(tags=("a", "a", "b", "b", "a"))
        tags = [r[0] for r in self.conn.execute("SELECT tag FROM note_tags WHERE note_id=?", (nid,))]
        self.assertEqual(sorted(tags), ["a", "b"])
        with self.assertRaises(ValidationError):
            self.note(tags=("a", "a", "a", "a", "a", "a"))

    def test_create_監査ログが同時に残る(self):
        nid = self.note(owner="bob")
        row = self.conn.execute("SELECT * FROM audit").fetchone()
        self.assertEqual((row["org_id"], row["actor_id"], row["action"], row["detail"]),
                         (1, 2, "create", f"note={nid}"))

    def test_create_create_tags失敗で何も残らない(self):
        before = snapshot(self.conn)
        self.fail_at("create:tags", RuntimeError("注入"))
        with self.assertRaises(RuntimeError):
            self.note(tags=("a", "b"))
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    def test_create_hookはタグ挿入前にメモ挿入済みの状態で呼ばれる(self):
        seen = []
        self.set_hook(lambda step: seen.append(
            (step, self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
             self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0],
             self.conn.in_transaction)))
        self.note(tags=("a",))
        self.assertEqual(seen, [("create:tags", 1, 0, True)])
