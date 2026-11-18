"""V6, V7, V8: 一覧・検索。"""
from notes import service
from notes.errors import ValidationError
from tests.helpers import BaseCase


class ListSearchBase(BaseCase):
    def set_updated(self, nid: int, ts: int) -> None:
        self.conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (ts, nid))


class ListTest(ListSearchBase):
    def test_updatedは新しい順_同時刻はid降順(self) -> None:
        a, b, c, d = (self.make(self.alice, title=t) for t in "ABCD")
        self.set_updated(a, 100)
        self.set_updated(b, 300)
        self.set_updated(c, 200)
        self.set_updated(d, 200)
        r = service.list_notes(self.conn, self.bob, sort="updated")
        self.assertEqual([i["id"] for i in r["items"]], [b, d, c, a])
        self.assertEqual(r["total"], 4)
        self.assertEqual(set(r["items"][0]), {"id", "title", "updated_at"})

    def test_titleは題名順_同題名はid昇順(self) -> None:
        x1 = self.make(self.alice, title="b")
        x2 = self.make(self.alice, title="a")
        x3 = self.make(self.alice, title="b")
        x4 = self.make(self.alice, title="c")
        r = service.list_notes(self.conn, self.bob, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [x2, x1, x3, x4])

    def test_ページングと戻り値(self) -> None:
        ids = [self.make(self.alice, title=f"t{i:02d}") for i in range(5)]
        r = service.list_notes(self.conn, self.bob, page=2, per_page=2, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], ids[2:4])
        self.assertEqual((r["total"], r["page"], r["per_page"]), (5, 2, 2))
        r = service.list_notes(self.conn, self.bob, page=3, per_page=2, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], ids[4:])
        self.assertEqual(service.list_notes(self.conn, self.bob, page=4, per_page=2)["items"], [])

    def test_巨大なpageでも落ちない(self) -> None:
        self.make(self.alice)
        r = service.list_notes(self.conn, self.bob, page=10**30, per_page=50)
        self.assertEqual((r["items"], r["total"]), ([], 1))

    def test_per_page50は可(self) -> None:
        self.assertEqual(service.list_notes(self.conn, self.bob, per_page=50)["per_page"], 50)


class ListValidationTest(ListSearchBase):
    """V7: 範囲外は ValidationError。"""

    def test_範囲外(self) -> None:
        for name, kw in [("page=0", {"page": 0}), ("page=-1", {"page": -1}),
                         ("per_page=0", {"per_page": 0}), ("per_page=51", {"per_page": 51}),
                         ("sort不正", {"sort": "id"}), ("sort大文字", {"sort": "Updated"}),
                         ("sort=None", {"sort": None}), ("page文字列", {"page": "1"}),
                         ("pageがbool", {"page": True}), ("per_pageが小数", {"per_page": 1.5})]:
            with self.subTest(name), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.bob, **kw)  # type: ignore[arg-type]


class SearchTest(ListSearchBase):
    def test_題名と本文の部分一致でid順(self) -> None:
        a = self.make(self.alice, title="りんご", body="x")
        b = self.make(self.alice, title="y", body="青りんごの話")
        self.make(self.alice, title="みかん", body="z")
        self.assertEqual(service.search_notes(self.conn, self.bob, "りんご"),
                         [{"id": a, "title": "りんご"}, {"id": b, "title": "y"}])

    def test_percentとunderscoreと円記号はリテラル(self) -> None:
        pct = self.make(self.alice, title="50% off")
        und = self.make(self.alice, title="a_b")
        bs = self.make(self.alice, title="p\\q")
        plain = self.make(self.alice, title="axb 5000 off")
        got = lambda q: [i["id"] for i in service.search_notes(self.conn, self.bob, q)]
        self.assertEqual(got("%"), [pct])
        self.assertEqual(got("_"), [und])  # plain の "axb" の x には一致しない
        self.assertEqual(got("a_b"), [und])
        self.assertEqual(got("\\"), [bs])
        self.assertEqual(got("50%"), [pct])
        self.assertNotIn(plain, got("5%f"))

    def test_タグはOR(self) -> None:
        a = self.make(self.alice, title="共通 a", tags=["red"])
        b = self.make(self.alice, title="共通 b", tags=["blue", "x"])
        self.make(self.alice, title="共通 c", tags=["green"])
        self.make(self.alice, title="共通 d")
        got = [i["id"] for i in service.search_notes(self.conn, self.bob, "共通", tags=["red", "blue"])]
        self.assertEqual(got, [a, b])
        # 重複指定・5個指定でも動く
        got = [i["id"] for i in service.search_notes(self.conn, self.bob, "共通", tags=["red", "red"])]
        self.assertEqual(got, [a])
        self.assertEqual(len(service.search_notes(self.conn, self.bob, "共通", tags=list("abcde"))), 0)

    def test_タグ指定でも他人の非公開と他組織は出ない(self) -> None:
        self.make(self.alice, title="共通", tags=["red"], private=True)
        self.make(self.carol, title="共通", tags=["red"])
        self.assertEqual(service.search_notes(self.conn, self.bob, "共通", tags=["red"]), [])

    def test_検索語の検証(self) -> None:
        for name, q in [("空", ""), ("空白のみ", "  　"), ("101文字", "a" * 101), ("None", None), ("数値", 1)]:
            with self.subTest(name), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.bob, q)  # type: ignore[arg-type]
        service.search_notes(self.conn, self.bob, "a" * 100)
        service.search_notes(self.conn, self.bob, " a ")  # 前後の空白は許容（空白のみでない）

    def test_タグはR1の規則で検査(self) -> None:
        for name, tags in [("6個", list("abcdef")), ("大文字", ["A"]), ("21文字", ["a" * 21]),
                           ("文字列単体", "abc"), ("不正文字", ["a_b"]), ("重複を数えて6個", ["a"] * 6)]:
            with self.subTest(name), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.bob, "q", tags=tags)  # type: ignore[arg-type]


class SearchInjectionTest(ListSearchBase):
    """V8: SQL 断片で他の結果が漏れない。"""

    def test_SQL断片(self) -> None:
        self.make(self.alice, title="公開A")
        self.make(self.alice, title="秘密", private=True)
        self.make(self.carol, title="他組織")
        for q in ["' OR 1=1 --", "' OR '1'='1", "x%' OR 1=1 --", "'; DROP TABLE notes; --",
                  "\" OR \"\"=\"", "') UNION SELECT id, body FROM notes --"]:
            with self.subTest(q):
                self.assertEqual(service.search_notes(self.conn, self.bob, q), [])
                self.assertEqual(service.search_notes(self.conn, self.bob, "公開", tags=[]),
                                 service.search_notes(self.conn, self.bob, "公開"))
        self.assertEqual(self.counts()["notes"], 3)

    def test_タグはSQL断片を受け付けない(self) -> None:
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.bob, "a", tags=["x') OR 1=1 --"])

    def test_題名にSQL断片を含むメモを保存して探せる(self) -> None:
        nid = self.make(self.alice, title="it's ' OR 1=1 --")
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.bob, "' OR 1=1 --")], [nid])
