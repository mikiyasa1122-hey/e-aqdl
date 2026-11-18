"""V1 V3 V12 V23: 入力検証・一覧検索・存在秘匿。"""
from notes import service
from notes.errors import NotFound, ValidationError
from tests.helpers import DbTestCase


class CreateValidation(DbTestCase):
    def test_題名の境界(self):
        for bad in ("", " ", " \t\n", "あ" * 101, None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, bad, "b")
        for ok in ("a", "あ" * 100):
            with self.subTest(ok=len(ok)):
                self.assertIsInstance(service.create_note(self.conn, self.alice, ok, "b"), int)

    def test_本文の境界(self):
        service.create_note(self.conn, self.alice, "t", "x" * 10000)
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "x" * 10001)
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", None)

    def test_タグの規則(self):
        service.create_note(self.conn, self.alice, "t", "b", tags=["a", "b", "c", "d", "e"])
        service.create_note(self.conn, self.alice, "t", "b", tags=["a" * 20, "0-9"])
        for bad in (["a"] * 6, ["a", "b", "c", "d", "e", "f"], ["A"], ["a_b"], [""],
                    ["a" * 21], ["a b"], "abc", ["é"], [1]):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, "t", "b", tags=bad)

    def test_重複タグは数えるが保存は除く(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", tags=["a", "a", "b", "b", "a"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["a", "b"])
        with self.assertRaises(ValidationError):  # 重複込みで6個は不可
            service.create_note(self.conn, self.alice, "t", "b", tags=["a", "a", "a", "a", "a", "a"])

    def test_失敗時に何も残らない(self):
        before = self.counts()
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "", "b", tags=["a"])
        self.assertEqual(self.counts(), before)

    def test_create_tagsフック失敗で何も残らない(self):
        def boom(step):
            if step == "create:tags":
                raise RuntimeError("注入")
        self.set_hook(boom)
        before = self.counts()
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", tags=["a", "b"])
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.conn.in_transaction)


class ListSearch(DbTestCase):
    def _mk(self, title, body="b", user=None, **kw):
        return service.create_note(self.conn, user or self.alice, title, body, **kw)

    def test_一覧の範囲検査(self):
        for kw in ({"page": 0}, {"page": -1}, {"per_page": 0}, {"per_page": 51},
                   {"sort": "id"}, {"sort": "title; DROP TABLE notes"}, {"page": "1"},
                   {"per_page": True}):
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **kw)
        self.assertEqual(service.list_notes(self.conn, self.alice, per_page=50)["per_page"], 50)
        self.assertEqual(service.list_notes(self.conn, self.alice, per_page=1)["per_page"], 1)

    def test_一覧の対象と並び順(self):
        a = self._mk("b題")
        b = self._mk("a題")
        c = self._mk("a題")
        self._mk("他人の非公開", user=self.bob, private=True)
        self._mk("他組織", user=self.eve)
        self.conn.execute("UPDATE notes SET updated_at = 100")
        r = service.list_notes(self.conn, self.alice, sort="updated")
        self.assertEqual([i["id"] for i in r["items"]], [c, b, a])  # 同秒は id 降順
        self.assertEqual(r["total"], 3)
        r = service.list_notes(self.conn, self.alice, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [b, c, a])  # 同題名は id 昇順
        self.conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (200, a))
        r = service.list_notes(self.conn, self.alice, sort="updated")
        self.assertEqual(r["items"][0]["id"], a)

    def test_ページング(self):
        ids = [self._mk(f"t{i}") for i in range(5)]
        self.conn.execute("UPDATE notes SET updated_at = 1")
        r = service.list_notes(self.conn, self.alice, page=2, per_page=2)
        self.assertEqual([i["id"] for i in r["items"]], [ids[2], ids[1]])
        self.assertEqual((r["total"], r["page"], r["per_page"]), (5, 2, 2))

    def test_自分の非公開は一覧に出る(self):
        n = self._mk("mine", private=True)
        self.assertEqual([i["id"] for i in service.list_notes(self.conn, self.alice)["items"]], [n])

    def test_検索の入力検査(self):
        for q in ("", "  ", "x" * 101, None, 3):
            with self.subTest(q=q), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, q)
        service.search_notes(self.conn, self.alice, "x" * 100)
        for tags in (["A"], ["a"] * 6, "abc", ["a_b"]):
            with self.subTest(tags=tags), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, "q", tags=tags)

    def test_パーセントとアンダースコアは文字(self):
        p = self._mk("100%達成")
        u = self._mk("a_b")
        self._mk("100X達成")
        self._mk("aXb")
        bs = self._mk("c\\d")
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "%")], [p])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "_")], [u])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "\\")], [bs])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "a_b")], [u])

    def test_題名か本文で部分一致しタグはOR(self):
        n1 = self._mk("hello", "x", tags=["t1"])
        n2 = self._mk("x", "say hello", tags=["t2"])
        n3 = self._mk("hello", "x", tags=["t3"])
        self._mk("hello", "x")
        ids = lambda **kw: [r["id"] for r in service.search_notes(self.conn, self.alice, "hello", **kw)]
        self.assertEqual(len(ids()), 4)
        self.assertEqual(ids(tags=["t1"]), [n1])
        self.assertEqual(ids(tags=["t1", "t2"]), [n1, n2])
        self.assertEqual(ids(tags=["t3", "zz"]), [n3])

    def test_検索は他人の非公開と他組織を含めない(self):
        self._mk("secret", user=self.bob, private=True)
        self._mk("secret", user=self.eve)
        self.assertEqual(service.search_notes(self.conn, self.alice, "secret"), [])

    def test_インジェクション文字列(self):
        n = self._mk("通常")
        for q in ("'; DROP TABLE notes; --", "' OR '1'='1", "%' OR 1=1 --"):
            with self.subTest(q=q):
                self.assertEqual(service.search_notes(self.conn, self.alice, q), [])
        self.assertEqual(service.search_notes(self.conn, self.alice, "通常"), [{"id": n, "title": "通常"}])
        # タグはパターン検査で拒否される
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.alice, "q", tags=["a') OR ('1'='1"])
        # 題名にインジェクション文字列があっても、そのまま保存され表は無事
        evil = self._mk("'; DROP TABLE notes; --")
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "DROP TABLE")], [evil])
        self.assertEqual(self.count("notes"), 2)


class ExistenceHiding(DbTestCase):
    def test_見られない3種は同じ型とメッセージ(self):
        other_org = service.create_note(self.conn, self.eve, "t", "b")
        private = service.create_note(self.conn, self.bob, "t", "b", private=True)
        errs = []
        for nid in (other_org, private, 9999):
            with self.assertRaises(NotFound) as cm:
                service.get_note(self.conn, self.alice, nid)
            errs.append(cm.exception)
        self.assertEqual({type(e) for e in errs}, {NotFound})
        self.assertEqual(len({str(e) for e in errs}), 1)
        self.assertEqual(self.note_row(private)["views"], 0)
