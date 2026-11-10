from notes import service
from notes.errors import ValidationError
from tests.helpers import DbTestCase


class ListTest(DbTestCase):
    def test_list_範囲外の入力はValidationError(self):
        u = self.u["alice"]
        bad = [{"page": 0}, {"page": -1}, {"page": True}, {"page": "1"}, {"page": 1.0},
               {"per_page": 0}, {"per_page": 51}, {"per_page": True}, {"per_page": "5"},
               {"sort": "id"}, {"sort": "title; DROP TABLE notes"}, {"sort": None},
               {"sort": "TITLE"}]
        for kw in bad:
            with self.assertRaises(ValidationError, msg=repr(kw)):
                service.list_notes(self.conn, u, **kw)

    def test_list_境界値は成功する(self):
        u = self.u["alice"]
        self.assertEqual(service.list_notes(self.conn, u, page=1, per_page=1)["per_page"], 1)
        self.assertEqual(service.list_notes(self.conn, u, page=1, per_page=50)["per_page"], 50)
        self.assertEqual(service.list_notes(self.conn, u, page=999)["items"], [])

    def test_list_並び順とページング(self):
        ids = [self.note(title=t) for t in ("b", "a", "c", "a")]
        for i, nid in enumerate(ids):
            self.conn.execute("UPDATE notes SET updated_at=? WHERE id=?", (100 if i < 2 else 200, nid))
        u = self.u["bob"]
        upd = service.list_notes(self.conn, u, sort="updated")
        self.assertEqual([i["id"] for i in upd["items"]], [ids[3], ids[2], ids[1], ids[0]])
        self.assertEqual(upd["total"], 4)
        self.assertEqual(set(upd["items"][0]), {"id", "title", "updated_at"})
        tit = service.list_notes(self.conn, u, sort="title")
        self.assertEqual([i["id"] for i in tit["items"]], [ids[1], ids[3], ids[0], ids[2]])
        p2 = service.list_notes(self.conn, u, page=2, per_page=3, sort="title")
        self.assertEqual([i["id"] for i in p2["items"]], [ids[2]])
        self.assertEqual((p2["page"], p2["per_page"], p2["total"]), (2, 3, 4))

    def test_list_他組織と他人の非公開は含めない(self):
        pub = self.note()
        mine = self.note(private=True)
        self.note(owner="bob", private=True)
        self.note(owner="carol")
        res = service.list_notes(self.conn, self.u["alice"])
        self.assertEqual(sorted(i["id"] for i in res["items"]), sorted([pub, mine]))
        self.assertEqual(res["total"], 2)
        res_a = service.list_notes(self.conn, self.u["admin"])
        self.assertEqual([i["id"] for i in res_a["items"]], [pub])


class SearchTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.n_pct = self.note(title="100% sure", body="alpha", tags=("x",))
        self.n_plain = self.note(title="100 sure", body="beta", tags=("y",))
        self.n_us = self.note(title="a_b", body="gamma", tags=("x", "y"))
        self.n_axb = self.note(title="axb", body="delta", tags=("z",))
        self.n_bs = self.note(title="p\\q", body="epsilon")
        self.n_body = self.note(title="zzz", body="has %percent inside", tags=())

    def ids(self, q, tags=(), who="alice"):
        return [r["id"] for r in service.search_notes(self.conn, self.u[who], q, tags)]

    def test_search_パーセントとアンダースコアは普通の文字(self):
        self.assertEqual(self.ids("%"), [self.n_pct, self.n_body])
        self.assertEqual(self.ids("_"), [self.n_us])
        self.assertEqual(self.ids("a_b"), [self.n_us])
        self.assertEqual(self.ids("\\"), [self.n_bs])
        self.assertEqual(self.ids("p\\q"), [self.n_bs])

    def test_search_題名か本文の部分一致でid順(self):
        self.assertEqual(self.ids("sure"), [self.n_pct, self.n_plain])
        self.assertEqual(self.ids("INSIDE"), [self.n_body])  # ASCII は大文字小文字を区別しない
        self.assertEqual(self.ids("nomatch"), [])
        self.assertEqual(set(service.search_notes(self.conn, self.u["alice"], "sure")[0]), {"id", "title"})

    def test_search_タグはOR(self):
        self.assertEqual(self.ids("a", ("x",)), [self.n_pct, self.n_us])
        self.assertEqual(self.ids("a", ("x", "z")), [self.n_pct, self.n_us, self.n_axb])
        self.assertEqual(self.ids("a", ("y", "z")), [self.n_plain, self.n_us, self.n_axb])
        self.assertEqual(self.ids("a", ("nope",)), [])

    def test_search_入力の検査(self):
        for q in ("", "   ", "x" * 101, None, 1):
            with self.assertRaises(ValidationError, msg=repr(q)):
                service.search_notes(self.conn, self.u["alice"], q)
        self.assertEqual(len(self.ids("x" * 100)), 0)
        for tags in ("abc", ("A",), ("a",) * 6, (1,), None):
            with self.assertRaises(ValidationError, msg=repr(tags)):
                service.search_notes(self.conn, self.u["alice"], "a", tags)

    def test_search_SQL断片は文字として扱われ表は壊れない(self):
        for q in ("' OR 1=1 --", "x'; DROP TABLE notes; --", "\" OR \"\"=\"", "%' OR '%'='", ") OR (1=1"):
            self.assertEqual(self.ids(q), [], msg=q)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 6)
        # 同じ語を題名に持つメモは、その文字列そのものでだけ見つかる
        nid = self.note(title="' OR 1=1 --")
        self.assertEqual(self.ids("' OR 1=1 --"), [nid])

    def test_search_他人の非公開と他組織は含めない(self):
        priv = self.note(title="secretword", private=True)
        other = self.note(owner="carol", title="secretword")
        self.assertEqual(self.ids("secretword", who="alice"), [priv])
        self.assertEqual(self.ids("secretword", who="bob"), [])
        self.assertEqual(self.ids("secretword", who="carol"), [other])

    def test_search_タグ付き検索でも他人の非公開を除く(self):
        self.note(title="tagged", tags=("x",), private=True)
        self.assertEqual(self.ids("tagged", ("x",), who="bob"), [])
