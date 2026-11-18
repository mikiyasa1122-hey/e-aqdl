"""AC15・AC16・K12: list_notes・search_notes の include_archived。"""
from notes import service
from notes.errors import ValidationError

from .base import Base


class ListSearchBase(Base):
    def setUp(self):
        super().setUp()
        c = self.conn
        self.ids = [service.create_note(c, self.alice, f"note{i}", f"body{i}", ["t"] if i % 2 else []) for i in range(6)]
        self.priv = service.create_note(c, self.alice, "note-priv", "b", private=True)
        self.other_org = service.create_note(c, self.eve, "note-eve", "b")
        for i in (1, 2):  # note1(タグ t)・note2 をアーカイブ
            service.archive_note(c, self.alice, self.ids[i])
        service.archive_note(c, self.alice, self.priv)
        service.archive_note(c, self.eve, self.other_org)

    def list_ids(self, user, **kw):
        r = service.list_notes(self.conn, user, per_page=50, **kw)
        return [i["id"] for i in r["items"]], r["total"]


class ListTest(ListSearchBase):
    def test_default_excludes_archived_from_items_and_total(self):
        ids, total = self.list_ids(self.alice)
        self.assertEqual(sorted(ids), sorted(set(self.ids) - {self.ids[1], self.ids[2]}))
        self.assertEqual(total, 4)

    def test_include_archived_true(self):
        ids, total = self.list_ids(self.alice, include_archived=True)
        self.assertEqual(sorted(ids), sorted(self.ids + [self.priv]))  # 他組織は含まない
        self.assertEqual(total, 7)
        ids, total = self.list_ids(self.bob, include_archived=True)
        self.assertNotIn(self.priv, ids)  # 他人の非公開は True でも出ない
        self.assertEqual(total, 6)

    def test_restore_brings_back(self):
        service.unarchive_note(self.conn, self.alice, self.ids[1])
        ids, total = self.list_ids(self.alice)
        self.assertIn(self.ids[1], ids)
        self.assertEqual(total, 5)

    def test_paging_is_based_on_non_archived(self):
        # 既定の件数は 4（id 0,3,4,5）。per_page=3 で 2 ページ
        p1 = service.list_notes(self.conn, self.alice, page=1, per_page=3, sort="title")
        p2 = service.list_notes(self.conn, self.alice, page=2, per_page=3, sort="title")
        p3 = service.list_notes(self.conn, self.alice, page=3, per_page=3, sort="title")
        self.assertEqual((len(p1["items"]), len(p2["items"]), len(p3["items"])), (3, 1, 0))
        self.assertEqual([p1["total"], p2["total"], p3["total"]], [4, 4, 4])
        self.assertEqual([i["title"] for i in p1["items"] + p2["items"]], ["note0", "note3", "note4", "note5"])
        # include_archived=True なら 7 件・3 ページ（最終ページは1件）
        q = service.list_notes(self.conn, self.alice, page=3, per_page=3, sort="title", include_archived=True)
        self.assertEqual((len(q["items"]), q["total"]), (1, 7))

    def test_all_archived_is_empty(self):
        for i in self.ids:
            if i not in (self.ids[1], self.ids[2]):
                service.archive_note(self.conn, self.alice, i)
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual((r["items"], r["total"]), ([], 0))
        self.assertEqual(service.list_notes(self.conn, self.alice, include_archived=True)["total"], 7)

    def test_sort_with_include_archived(self):
        r = service.list_notes(self.conn, self.alice, sort="title", include_archived=True)
        self.assertEqual([i["title"] for i in r["items"]], sorted(i["title"] for i in r["items"]))


class SearchTest(ListSearchBase):
    def ids_of(self, user, q, **kw):
        return [r["id"] for r in service.search_notes(self.conn, user, q, **kw)]

    def test_default_excludes_archived(self):
        self.assertEqual(self.ids_of(self.alice, "note"), [self.ids[0], self.ids[3], self.ids[4], self.ids[5]])

    def test_include_archived_true_keeps_visibility(self):
        self.assertEqual(self.ids_of(self.alice, "note", include_archived=True), self.ids + [self.priv])
        self.assertEqual(self.ids_of(self.bob, "note", include_archived=True), self.ids)  # 他人の非公開・他組織は出ない
        self.assertEqual(self.ids_of(self.eve, "note", include_archived=True), [self.other_org])

    def test_with_tags(self):
        # タグ t を持つのは note1(アーカイブ)・note3・note5
        self.assertEqual(self.ids_of(self.alice, "note", tags=["t"]), [self.ids[3], self.ids[5]])
        self.assertEqual(self.ids_of(self.alice, "note", tags=["t"], include_archived=True),
                         [self.ids[1], self.ids[3], self.ids[5]])

    def test_restore_brings_back(self):
        service.unarchive_note(self.conn, self.alice, self.ids[1])
        self.assertIn(self.ids[1], self.ids_of(self.alice, "note"))

    def test_wildcard_terms(self):
        a = service.create_note(self.conn, self.alice, "100%_done", "x")
        service.archive_note(self.conn, self.alice, a)
        service.create_note(self.conn, self.alice, "100 abc", "x")
        self.assertEqual(self.ids_of(self.alice, "100%_"), [])
        self.assertEqual(self.ids_of(self.alice, "100%_", include_archived=True), [a])


class InjectionAndConsistencyTest(ListSearchBase):
    def test_hostile_search_terms(self):
        for q in ("%", "_", "'", '"', "x'; DROP TABLE notes;--", "' OR '1'='1", "\\"):
            for inc in (False, True):
                with self.subTest(q=q, inc=inc):
                    self.assertEqual(service.search_notes(self.conn, self.alice, q, include_archived=inc), [])
        self.assertEqual(self.count("notes"), 8)

    def test_hostile_tags(self):
        for t in ("x' OR '1'='1", "t;--", "%"):
            with self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, "note", tags=[t])

    def test_total_matches_items_for_any_archive_count(self):
        for inc in (False, True):
            for archived_more in range(4):
                with self.subTest(inc=inc, n=archived_more):
                    for i in (self.ids[0], self.ids[3], self.ids[4], self.ids[5]):
                        if self.conn.execute("SELECT archived FROM notes WHERE id=?", (i,)).fetchone()[0]:
                            service.unarchive_note(self.conn, self.alice, i)
                    for i in (self.ids[0], self.ids[3], self.ids[4], self.ids[5])[:archived_more]:
                        service.archive_note(self.conn, self.alice, i)
                    r = service.list_notes(self.conn, self.alice, per_page=2, include_archived=inc)
                    seen, page = [], 1
                    while True:
                        p = service.list_notes(self.conn, self.alice, page=page, per_page=2, include_archived=inc)
                        if not p["items"]:
                            break
                        seen += [i["id"] for i in p["items"]]
                        page += 1
                    self.assertEqual(len(seen), r["total"])
                    self.assertEqual(len(set(seen)), len(seen))

    def test_include_archived_must_be_bool(self):
        for bad in (1, 0, "yes", "True", None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    service.list_notes(self.conn, self.alice, include_archived=bad)
                with self.assertRaises(ValidationError):
                    service.search_notes(self.conn, self.alice, "note", include_archived=bad)
