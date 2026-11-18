"""AC6 AC7 AC18: 一覧・検索。"""
from unittest import mock

from notes import service
from notes.errors import ValidationError
from tests.helpers import MemCase


class ListSearchUnit(MemCase):
    def _make(self, specs):
        """specs: [(title, updated_at)] を順に作る。id のリストを返す。"""
        ids = []
        for title, ts in specs:
            with mock.patch.object(service, "_now", return_value=ts):
                ids.append(self.note(title=title))
        return ids

    def test_updated順は新しい順で同値はid降順(self):  # V6
        a, b, c, d = self._make([("a", 100), ("b", 300), ("c", 300), ("d", 200)])
        res = service.list_notes(self.conn, self.alice, sort="updated")
        self.assertEqual([i["id"] for i in res["items"]], [c, b, d, a])
        self.assertEqual(res["total"], 4)
        self.assertEqual(set(res["items"][0]), {"id", "title", "updated_at"})

    def test_title順は題名順で同値はid昇順(self):  # V6
        ids = self._make([("b", 1), ("a", 1), ("b", 1), ("C", 1)])
        res = service.list_notes(self.conn, self.alice, sort="title")
        # BINARY 照合なので大文字 C は小文字 a・b より前
        self.assertEqual([i["id"] for i in res["items"]], [ids[3], ids[1], ids[0], ids[2]])

    def test_ページ境界とtotal(self):  # V6
        ids = self._make([(f"t{i}", 100 + i) for i in range(5)])
        newest = list(reversed(ids))
        cases = [
            (1, 2, newest[0:2]), (2, 2, newest[2:4]), (3, 2, newest[4:5]),
            (4, 2, []), (1, 5, newest), (2, 5, []), (10 ** 30, 100, []),
        ]
        for page, per, expect in cases:
            with self.subTest(page=page, per_page=per):
                res = service.list_notes(self.conn, self.alice, page=page, per_page=per)
                self.assertEqual([i["id"] for i in res["items"]], expect)
                self.assertEqual((res["total"], res["page"], res["per_page"]), (5, page, per))

    def test_per_page上限100は成功(self):  # V6
        self.assertEqual(service.list_notes(self.conn, self.alice, per_page=100)["per_page"], 100)

    def test_不正引数(self):  # V6
        bad = [{"page": 0}, {"page": -1}, {"per_page": 0}, {"per_page": 101},
               {"sort": "id"}, {"sort": None}, {"sort": ["updated"]}, {"page": "1"},
               {"page": True}, {"per_page": 1.5}, {"sort": "updated; DROP TABLE notes"}]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **kw)

    def test_閲覧可能なものだけ(self):  # V6
        self.note(self.alice, "公開")
        self.note(self.alice, "alice非公開", private=True)
        self.note(self.bob, "bob非公開", private=True)
        self.note(self.carol, "他組織")
        res = service.list_notes(self.conn, self.alice)
        self.assertEqual(sorted(i["title"] for i in res["items"]), ["alice非公開", "公開"])
        self.assertEqual(res["total"], 2)

    def test_部分一致と大小文字(self):  # V7
        a = self.note(title="Hello World", body="x")
        b = self.note(title="x", body="say HELLO there")
        self.note(title="none", body="none")
        for q in ("hello", "HELLO", "lo wo", "Hello"):
            with self.subTest(q=q):
                ids = [r["id"] for r in service.search_notes(self.conn, self.alice, q)]
                self.assertEqual(ids, [a, b] if q != "lo wo" else [a])
        res = service.search_notes(self.conn, self.alice, "hello")
        self.assertEqual(res[0], {"id": a, "title": "Hello World"})

    def test_ワイルドカードはリテラル(self):  # V7
        pct = self.note(title="100% done")
        und = self.note(title="a_b")
        self.note(title="aXb")
        self.note(title="back\\slash")
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "%")], [pct])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "_")], [und])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "a_b")], [und])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "\\")), 1)
        self.assertEqual(service.search_notes(self.conn, self.alice, "%%"), [])

    def test_タグは全てを持つものだけ(self):  # V7
        a = self.note(title="k", tags=["x", "y"])
        self.note(title="k", tags=["x"])
        self.note(title="k", tags=["y", "z"])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "k", ["x", "y"])], [a])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "k", ["x"])), 2)
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "k")), 3)
        self.assertEqual(service.search_notes(self.conn, self.alice, "k", ["x", "nope"]), [])

    def test_検索の不正引数(self):  # V7 V23
        bad = [{"q": ""}, {"q": "   "}, {"q": None}, {"q": 1}, {"q": "a" * 201},
               {"q": "\ud800"}, {"q": "a", "tags": "abc"}, {"q": "a", "tags": [1]},
               {"q": "a", "tags": [f"t{i}" for i in range(11)]}, {"q": "a", "tags": None}]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, **kw)
        self.assertEqual(service.search_notes(self.conn, self.alice, "a" * 200), [])

    def test_検索はid順(self):  # V7
        ids = [self.note(title="same") for _ in range(4)]
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "same")], ids)

    def test_SQL注入の検索語とタグ(self):  # V18
        a = self.note(title="it's", tags=["a'b"])
        for q in ("' OR '1'='1", "x'; DROP TABLE notes; --", "\" OR 1=1 --"):
            with self.subTest(q=q):
                self.assertEqual(service.search_notes(self.conn, self.alice, q), [])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "it's", ["a'b"])], [a])
        self.assertEqual(service.search_notes(self.conn, self.alice, "it", ["a' OR '1'='1"]), [])
        self.assertEqual(len(service.list_notes(self.conn, self.alice)["items"]), 1)
