"""V3 / V19: R3 一覧・検索（範囲検査・LIKE エスケープ・プレースホルダー）。"""
from notes import service
from notes.errors import ValidationError
from tests.support import BaseCase, count


class ListSearchTest(BaseCase):
    def make(self, user="alice", title="t", body="b", **kw):
        return service.create_note(self.conn, self.u[user], title, body, **kw)

    def set_updated(self, nid, ts):
        self.conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (ts, nid))

    def lst(self, **kw):
        return service.list_notes(self.conn, self.u["alice"], **kw)

    def test_unit_list_不正な入力はValidationErrorでDBは無傷(self):
        self.make()
        bad = [{"page": 0}, {"page": -1}, {"page": True}, {"page": "1"}, {"page": 1.0},
               {"per_page": 0}, {"per_page": 51}, {"per_page": True}, {"per_page": "5"},
               {"sort": "id"}, {"sort": "updated; DROP TABLE notes"}, {"sort": None}, {"sort": ["updated"]}]
        for kw in bad:
            with self.assertRaises(ValidationError, msg=repr(kw)):
                self.lst(**kw)
        self.assertEqual(count(self.conn, "notes"), 1)

    def test_unit_list_境界値は通る(self):
        self.make()
        self.assertEqual(self.lst(page=1, per_page=1)["per_page"], 1)
        self.assertEqual(self.lst(page=1, per_page=50)["per_page"], 50)
        r = self.lst(page=99)
        self.assertEqual((r["items"], r["total"], r["page"]), ([], 1, 99))

    def test_unit_list_並び順の同点規則(self):
        ids = [self.make(title="same") for _ in range(3)]
        for i in ids:
            self.set_updated(i, 100)
        self.assertEqual([i["id"] for i in self.lst(sort="updated")["items"]], ids[::-1])
        self.assertEqual([i["id"] for i in self.lst(sort="title")["items"]], ids)
        a = self.make(title="aaa")
        z = self.make(title="zzz")
        self.set_updated(a, 1)
        self.set_updated(z, 999)
        self.assertEqual(self.lst(sort="updated")["items"][0]["id"], z)
        order = [i["id"] for i in self.lst(sort="title")["items"]]
        self.assertEqual(order[0], a)
        self.assertEqual(order[-1], z)

    def test_unit_list_ページ分割と件数(self):
        ids = [self.make(title=f"n{i:02d}") for i in range(5)]
        got = []
        for page in (1, 2, 3):
            r = self.lst(page=page, per_page=2, sort="title")
            self.assertEqual(r["total"], 5)
            self.assertEqual((r["page"], r["per_page"]), (page, 2))
            got.append([i["id"] for i in r["items"]])
        self.assertEqual(got, [ids[0:2], ids[2:4], ids[4:5]])
        item = self.lst()["items"][0]
        self.assertEqual(set(item), {"id", "title", "updated_at"})

    def test_unit_list_他組織と他人の非公開は出ない(self):
        pub = self.make()
        self.make(user="bob", private=True)
        bob_pub = self.make(user="bob")
        self.make(user="carol")
        r = self.lst()
        self.assertEqual(sorted(i["id"] for i in r["items"]), [pub, bob_pub])
        self.assertEqual(r["total"], 2)
        # 自分の非公開は見える
        mine = self.make(private=True)
        self.assertIn(mine, [i["id"] for i in self.lst()["items"]])

    def search(self, q, tags=(), user="alice"):
        return [i["id"] for i in service.search_notes(self.conn, self.u[user], q, tags)]

    def test_unit_search_ワイルドカードは普通の文字(self):
        pct = self.make(title="100% done")
        us = self.make(title="a_b")
        bs = self.make(title="back\\slash")
        plain = self.make(title="100 xdone", body="axb")
        self.assertEqual(self.search("100%"), [pct])
        self.assertEqual(self.search("%"), [pct])
        self.assertEqual(self.search("a_b"), [us])
        self.assertEqual(self.search("_"), [us])
        self.assertEqual(self.search("\\"), [bs])
        self.assertEqual(self.search("k\\s"), [bs])
        self.assertNotIn(plain, self.search("_"))

    def test_unit_search_本文も対象で結果はid順(self):
        a = self.make(title="x", body="hello world")
        b = self.make(title="hello", body="y")
        self.make(title="zzz", body="zzz")
        self.assertEqual(self.search("hello"), [a, b])
        self.assertEqual(set(service.search_notes(self.conn, self.u["alice"], "hello")[0]), {"id", "title"})

    def test_unit_search_インジェクションは何も壊さない(self):
        self.make(title="plain")
        self.assertEqual(self.search("' OR 1=1 --"), [])
        self.assertEqual(self.search("'; DROP TABLE notes; --"), [])
        self.assertEqual(count(self.conn, "notes"), 1)
        with self.assertRaises(ValidationError):
            self.search("x", tags=["a') OR 1=1 --"])

    def test_unit_search_検索語の境界(self):
        n = self.make(title="a" * 100)
        self.assertEqual(self.search("a"), [n])
        self.assertEqual(self.search("a" * 100), [n])
        for q in ("", " ", "  \t", "a" * 101, None, 5):
            with self.assertRaises(ValidationError, msg=repr(q)):
                self.search(q)

    def test_unit_search_タグはOR(self):
        a = self.make(title="m", tags=["red"])
        b = self.make(title="m", tags=["blue", "x"])
        c = self.make(title="m", tags=["green"])
        d = self.make(title="m")
        self.assertEqual(self.search("m", ["red"]), [a])
        self.assertEqual(self.search("m", ["red", "blue"]), [a, b])
        self.assertEqual(self.search("m", ["blue", "x"]), [b])
        self.assertEqual(self.search("m", ["none"]), [])
        self.assertEqual(self.search("m"), [a, b, c, d])

    def test_unit_search_不正タグはValidationError(self):
        for tags in (["A"], list("abcdef"), "abc", [1], ["a" * 21]):
            with self.assertRaises(ValidationError, msg=repr(tags)):
                self.search("m", tags)

    def test_unit_search_他組織と他人の非公開は出ない(self):
        mine = self.make(title="secret")
        self.make(user="bob", title="secret", private=True)
        self.make(user="carol", title="secret")
        self.assertEqual(self.search("secret"), [mine])
        self.assertEqual(self.search("secret", user="carol"), [mine + 2])
