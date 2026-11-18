"""R13・AC14・V17〜V19・V25: list・search の include_archived。"""
from notes import service
from notes.errors import ValidationError

from .base import Base


class ArchiveListTest(Base):
    def setUp(self):
        super().setUp()
        mk = lambda u, t, b="body", tags=(), private=False: service.create_note(self.conn, u, t, b, tags, private)
        self.a1 = mk(self.alice, "alpha", tags=["x"])
        self.a2 = mk(self.alice, "beta", tags=["y"])
        self.a3 = mk(self.alice, "gamma", tags=["x"])
        self.priv = mk(self.bob, "secret-alpha", private=True, tags=["x"])
        self.other = mk(self.eve, "alpha-other", tags=["x"])
        for nid, u in ((self.a2, self.alice), (self.priv, self.bob), (self.other, self.eve)):
            service.archive_note(self.conn, u, nid)
        self.conn.execute("UPDATE notes SET updated_at = id * 10")

    def ids(self, r):
        return [i["id"] for i in r["items"]]

    def test_list_default_excludes_archived_and_total(self):
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual((self.ids(r), r["total"]), ([self.a3, self.a1], 2))

    def test_list_include_true_keeps_privacy_and_org(self):
        r = service.list_notes(self.conn, self.alice, include_archived=True)
        self.assertEqual((self.ids(r), r["total"]), ([self.a3, self.a2, self.a1], 3))  # 他人の非公開・他組織は出ない

    def test_list_paging_and_sort(self):
        r = service.list_notes(self.conn, self.alice, page=2, per_page=2, include_archived=True)
        self.assertEqual((self.ids(r), r["total"]), ([self.a1], 3))
        r = service.list_notes(self.conn, self.alice, page=2, per_page=1)
        self.assertEqual((self.ids(r), r["total"]), ([self.a1], 2))
        r = service.list_notes(self.conn, self.alice, page=3, per_page=1)
        self.assertEqual((self.ids(r), r["total"]), ([], 2))
        r = service.list_notes(self.conn, self.alice, sort="title", include_archived=True)
        self.assertEqual(self.ids(r), [self.a1, self.a2, self.a3])
        r = service.list_notes(self.conn, self.alice, sort="title")
        self.assertEqual(self.ids(r), [self.a1, self.a3])

    def test_owner_sees_own_private_archived_only_with_flag(self):
        self.assertEqual(self.ids(service.list_notes(self.conn, self.bob)), [self.a3, self.a1])
        self.assertIn(self.priv, self.ids(service.list_notes(self.conn, self.bob, include_archived=True)))

    def test_search_default_and_include(self):
        s = lambda **k: [r["id"] for r in service.search_notes(self.conn, self.alice, "a", **k)]
        self.assertEqual(s(), [self.a1, self.a3])
        self.assertEqual(s(include_archived=True), [self.a1, self.a2, self.a3])  # 他人の非公開・他組織は出ない
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "alpha", include_archived=True)],
                         [self.a1])

    def test_search_tags_or_and_body(self):
        r = service.search_notes(self.conn, self.alice, "body", tags=["x", "y"])
        self.assertEqual([i["id"] for i in r], [self.a1, self.a3])
        r = service.search_notes(self.conn, self.alice, "body", tags=["x", "y"], include_archived=True)
        self.assertEqual([i["id"] for i in r], [self.a1, self.a2, self.a3])
        r = service.search_notes(self.conn, self.alice, "body", tags=["y"])
        self.assertEqual(r, [])

    def test_search_wildcards_with_archived(self):
        n1 = service.create_note(self.conn, self.alice, "100%_done", "b")
        n2 = service.create_note(self.conn, self.alice, "100xxdone", "b")
        service.archive_note(self.conn, self.alice, n1)
        self.assertEqual(service.search_notes(self.conn, self.alice, "%_"), [])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "%_", include_archived=True)], [n1])
        self.assertNotIn(n2, [r["id"] for r in service.search_notes(self.conn, self.alice, "%_", include_archived=True)])

    def test_invalid_include_archived_type(self):
        for bad in (1, 0, "yes", None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    service.list_notes(self.conn, self.alice, include_archived=bad)
                with self.assertRaises(ValidationError):
                    service.search_notes(self.conn, self.alice, "a", include_archived=bad)

    def test_unarchive_returns_to_default(self):
        service.unarchive_note(self.conn, self.alice, self.a2)
        self.assertEqual(self.ids(service.list_notes(self.conn, self.alice)), [self.a3, self.a2, self.a1])
        self.assertIn(self.a2, [r["id"] for r in service.search_notes(self.conn, self.alice, "beta")])

    def test_regression_defaults_unchanged(self):
        """アーカイブがなければ従来どおりの結果（V25）。"""
        r = service.list_notes(self.conn, self.eve)
        self.assertEqual(set(r), {"items", "total", "page", "per_page"})
        self.assertEqual(set(r["items"][0]) if r["items"] else {"id", "title", "updated_at"}, {"id", "title", "updated_at"})
        self.assertEqual(self.ids(service.list_notes(self.conn, self.eve, include_archived=True)), [self.other])
