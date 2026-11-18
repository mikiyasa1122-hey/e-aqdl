"""AC3・K1・K2: R3 の規則どおりに一覧・検索の結果と入力の拒否が決まる。"""
from notes import service
from notes.errors import ValidationError

from .base import Base


class ListTest(Base):
    def setUp(self):
        super().setUp()
        self.ids = [service.create_note(self.conn, self.alice, t, "b") for t in ("c", "a", "b")]
        service.create_note(self.conn, self.bob, "bob-private", "b", private=True)
        service.create_note(self.conn, self.eve, "other-org", "b")

    def titles(self, user, **kw):
        return [r["title"] for r in service.list_notes(self.conn, user, **kw)["items"]]

    def test_scope_excludes_other_org_and_others_private(self):
        res = service.list_notes(self.conn, self.alice)
        self.assertEqual(res["total"], 3)
        self.assertNotIn("bob-private", self.titles(self.alice))
        self.assertNotIn("other-org", self.titles(self.alice))
        self.assertIn("bob-private", self.titles(self.bob))
        self.assertEqual(service.list_notes(self.conn, self.admin)["total"], 3)  # 管理者にも他人の非公開は見えない

    def test_sort(self):
        self.assertEqual(self.titles(self.alice, sort="title"), ["a", "b", "c"])
        self.conn.execute("UPDATE notes SET updated_at = id")  # 作成順に時刻を揃えて、新しい順を確かめる
        self.assertEqual(self.titles(self.alice, sort="updated"), ["b", "a", "c"])

    def test_paging(self):
        self.assertEqual(self.titles(self.alice, sort="title", page=1, per_page=2), ["a", "b"])
        self.assertEqual(self.titles(self.alice, sort="title", page=2, per_page=2), ["c"])
        res = service.list_notes(self.conn, self.alice, page=3, per_page=2)
        self.assertEqual((res["items"], res["total"], res["page"], res["per_page"]), ([], 3, 3, 2))
        service.list_notes(self.conn, self.alice, per_page=50)

    def test_huge_page_returns_no_items(self):
        res = service.list_notes(self.conn, self.alice, page=10 ** 30, per_page=50)
        self.assertEqual((res["items"], res["total"]), ([], 3))

    def test_invalid_paging_and_sort(self):
        bad = [dict(page=0), dict(page=-1), dict(page="1"), dict(page=True), dict(page=1.0), dict(per_page=0),
               dict(per_page=51), dict(per_page=False), dict(per_page="20"), dict(sort="id"), dict(sort="title; DROP")]
        for kw in bad:
            with self.subTest(**{k: repr(v) for k, v in kw.items()}), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **kw)


class SearchTest(Base):
    def setUp(self):
        super().setUp()
        self.a = service.create_note(self.conn, self.alice, "100% done", "x", ["work"])
        self.b = service.create_note(self.conn, self.alice, "1000 done", "file_name", ["home"])
        self.c = service.create_note(self.conn, self.alice, "plain", "filexname", ["work", "home"])
        service.create_note(self.conn, self.bob, "secret 100%", "x", private=True)
        service.create_note(self.conn, self.eve, "100% other org", "x")

    def ids(self, q, tags=(), user=None):
        return [r["id"] for r in service.search_notes(self.conn, user or self.alice, q, tags)]

    def test_partial_match_in_title_or_body(self):
        self.assertEqual(self.ids("done"), [self.a, self.b])
        self.assertEqual(self.ids("name"), [self.b, self.c])

    def test_percent_and_underscore_are_literal(self):
        self.assertEqual(self.ids("100%"), [self.a])
        self.assertEqual(self.ids("%"), [self.a])
        self.assertEqual(self.ids("file_name"), [self.b])
        self.assertEqual(self.ids("_"), [self.b])
        self.assertEqual(self.ids("\\"), [])

    def test_scope(self):
        self.assertNotIn("secret 100%", [r["title"] for r in service.search_notes(self.conn, self.alice, "100")])
        self.assertEqual(len(service.search_notes(self.conn, self.bob, "100")), 3)  # 自分の非公開は含む
        self.assertEqual(len(service.search_notes(self.conn, self.eve, "100")), 1)

    def test_tag_filter_is_or(self):
        self.assertEqual(self.ids("o", ["work"]), [self.a])
        self.assertEqual(self.ids("e", ["work", "home"]), [self.a, self.b, self.c])
        self.assertEqual(self.ids("e", ["nothing"]), [])

    def test_sql_text_is_just_a_value(self):
        self.assertEqual(self.ids("' OR 1=1 --"), [])
        self.assertEqual(self.ids("x", ["a"]), [])
        self.assertEqual(self.count("notes"), 5)

    def test_invalid_query_and_tags(self):
        for q in ("", "   ", "q" * 101, None, 1):
            with self.subTest(q=q), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, q)
        self.ids("q" * 100)
        for tags in (["BAD"], ["a"] * 6, "work", ["abc\n"], [1]):
            with self.subTest(tags=tags), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, "x", tags)
