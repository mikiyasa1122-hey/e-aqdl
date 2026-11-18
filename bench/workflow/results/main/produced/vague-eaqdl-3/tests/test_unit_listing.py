"""V5 V6 V7: タグ・検索・一覧・ページ。"""
from typing import Any

from notes import service
from notes.errors import ValidationError
from tests.helpers import NotesTestCase


class TagSearchTest(NotesTestCase):
    def test_tags_名前順で返り重複は1つ(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b", tags=["zeta", "alpha", "zeta", "Mid"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["Mid", "alpha", "zeta"])

    def test_search_題名と本文の部分一致でid順(self) -> None:
        a = service.create_note(self.conn, self.alice, "りんご", "x")
        b = service.create_note(self.conn, self.alice, "x", "青いりんごの話")
        service.create_note(self.conn, self.alice, "みかん", "y")
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "りんご")], [a, b])
        self.assertEqual(service.search_notes(self.conn, self.alice, "りんご")[0], {"id": a, "title": "りんご"})

    def test_search_タグ指定は全タグを持つメモだけ(self) -> None:
        both = service.create_note(self.conn, self.alice, "A", "b", tags=["x", "y"])
        service.create_note(self.conn, self.alice, "B", "b", tags=["x"])
        service.create_note(self.conn, self.alice, "C", "b", tags=["y", "z"])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "", tags=["x", "y"])], [both])
        # 重複タグは1つにまとめる（件数条件が狂わない）
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "", tags=["x", "y", "x"])],
                         [both])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "", tags=["x"])), 2)

    def test_search_検索語とタグの両方を満たす(self) -> None:
        hit = service.create_note(self.conn, self.alice, "りんご", "b", tags=["x"])
        service.create_note(self.conn, self.alice, "りんご", "b", tags=["y"])
        service.create_note(self.conn, self.alice, "みかん", "b", tags=["x"])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "りんご", tags=["x"])], [hit])

    def test_search_特殊文字は文字として扱う(self) -> None:
        pct = service.create_note(self.conn, self.alice, "100% 達成", "b")
        und = service.create_note(self.conn, self.alice, "a_b", "b")
        quote = service.create_note(self.conn, self.alice, "it's", "b")
        service.create_note(self.conn, self.alice, "axb", "100 達成")
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "%")], [pct])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "_")], [und])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "a_b")], [und])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "'")], [quote])


class ListTest(NotesTestCase):
    def _make(self, n: int) -> list[int]:
        return [service.create_note(self.conn, self.alice, f"t{i:02d}", "b") for i in range(n)]

    def test_list_updated順は新しい順で同値はid降順(self) -> None:
        ids = self._make(4)
        # id0,1 を同じ時刻、id2 を新しく、id3 を古くする
        for nid, ts in zip(ids, (100, 100, 200, 50)):
            self.conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (ts, nid))
        got = [i["id"] for i in service.list_notes(self.conn, self.alice, sort="updated")["items"]]
        self.assertEqual(got, [ids[2], ids[1], ids[0], ids[3]])
        item = service.list_notes(self.conn, self.alice)["items"][0]
        self.assertEqual(set(item), {"id", "title", "updated_at"})

    def test_list_title順は題名順で同値はid昇順(self) -> None:
        a = service.create_note(self.conn, self.alice, "b", "x")
        b = service.create_note(self.conn, self.alice, "a", "x")
        c = service.create_note(self.conn, self.alice, "b", "x")
        got = [i["id"] for i in service.list_notes(self.conn, self.alice, sort="title")["items"]]
        self.assertEqual(got, [b, a, c])

    def test_list_ページ境界(self) -> None:
        ids = self._make(5)
        by_title = ids  # t00..t04 は題名順 = id 順
        p1 = service.list_notes(self.conn, self.alice, page=1, per_page=2, sort="title")
        p3 = service.list_notes(self.conn, self.alice, page=3, per_page=2, sort="title")
        self.assertEqual([i["id"] for i in p1["items"]], by_title[:2])
        self.assertEqual([i["id"] for i in p3["items"]], by_title[4:])
        self.assertEqual((p1["total"], p1["page"], p1["per_page"]), (5, 1, 2))
        # ちょうど最終ページの次は空
        p4 = service.list_notes(self.conn, self.alice, page=4, per_page=2, sort="title")
        self.assertEqual((p4["items"], p4["total"]), ([], 5))
        exact = service.list_notes(self.conn, self.alice, page=1, per_page=5)
        self.assertEqual(len(exact["items"]), 5)
        self.assertEqual(service.list_notes(self.conn, self.alice, page=2, per_page=5)["items"], [])

    def test_list_範囲外pageと巨大値は空で例外にならない(self) -> None:
        self._make(3)
        for page, per in ((100, 20), (10**30, 20), (1, 10**30), (10**30, 10**30)):
            got = service.list_notes(self.conn, self.alice, page=page, per_page=per)
            self.assertEqual(got["total"], 3)
            if page == 1:
                self.assertEqual(len(got["items"]), 3)
            else:
                self.assertEqual(got["items"], [])

    def test_list_不正値はValidationError(self) -> None:
        self._make(1)
        bad: list[dict[str, Any]] = [{"page": 0}, {"page": -1}, {"per_page": 0}, {"per_page": -5}, {"sort": "id"},
               {"sort": None}, {"page": "1"}, {"per_page": 1.5}, {"page": True}, {"sort": "updated; DROP"}]
        for kwargs in bad:
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                service.list_notes(self.conn, self.alice, **kwargs)

    def test_list_空でも動く(self) -> None:
        self.assertEqual(service.list_notes(self.conn, self.alice),
                         {"items": [], "total": 0, "page": 1, "per_page": 20})
