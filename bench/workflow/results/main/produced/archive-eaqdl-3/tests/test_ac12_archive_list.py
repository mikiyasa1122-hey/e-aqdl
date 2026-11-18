"""AC15・AC16・AC19（単体）: 一覧・検索の既定／include_archived、get_note、SQL 注入、ログ。"""
from notes import service
from notes.errors import ValidationError

from .base import Base


class ArchiveListTest(Base):
    def setUp(self):
        super().setUp()
        c, a = self.conn, self.alice
        self.n_live = service.create_note(c, a, "alpha live", "x", tags=["t1"])
        self.n_arch = service.create_note(c, a, "alpha arch", "x", tags=["t1"])
        self.n_other_priv = service.create_note(c, self.bob, "alpha bobpriv", "x", tags=["t1"], private=True)
        self.n_other_priv_arch = service.create_note(c, self.bob, "alpha bobpriv2", "x", tags=["t1"], private=True)
        self.n_eve = service.create_note(c, self.eve, "alpha eve", "x", tags=["t1"])
        self.n_eve_arch = service.create_note(c, self.eve, "alpha eve2", "x", tags=["t1"])
        service.archive_note(c, a, self.n_arch)
        service.archive_note(c, self.bob, self.n_other_priv_arch)
        service.archive_note(c, self.eve, self.n_eve_arch)

    def ids(self, **kw):
        r = service.list_notes(self.conn, self.alice, per_page=50, **kw)
        return sorted(i["id"] for i in r["items"]), r["total"]

    def test_v8_v9_list_default_true_and_restore(self):
        self.assertEqual(self.ids(), ([self.n_live], 1))
        self.assertEqual(self.ids(include_archived=True), (sorted([self.n_live, self.n_arch]), 2))
        service.unarchive_note(self.conn, self.alice, self.n_arch)
        self.assertEqual(self.ids(), (sorted([self.n_live, self.n_arch]), 2))

    def test_v9_total_matches_items_with_pagination(self):
        r = service.list_notes(self.conn, self.alice, page=1, per_page=1)
        self.assertEqual((len(r["items"]), r["total"]), (1, 1))
        r = service.list_notes(self.conn, self.alice, page=1, per_page=1, include_archived=True)
        self.assertEqual((len(r["items"]), r["total"]), (1, 2))

    def test_v8_search_default_true_with_tags(self):
        s = lambda **kw: [r["id"] for r in service.search_notes(self.conn, self.alice, "alpha", **kw)]
        self.assertEqual(s(), [self.n_live])
        self.assertEqual(s(tags=["t1"]), [self.n_live])
        self.assertEqual(s(include_archived=True), [self.n_live, self.n_arch])
        self.assertEqual(s(tags=["t1"], include_archived=True), [self.n_live, self.n_arch])
        service.unarchive_note(self.conn, self.alice, self.n_arch)
        self.assertEqual(s(tags=["t1"]), [self.n_live, self.n_arch])

    def test_v9_search_parentheses_keep_visibility(self):
        # 題名が一致する他人の非公開・他組織は、OR の優先順位が崩れると漏れる
        for kw in ({}, {"include_archived": True}):
            ids = [r["id"] for r in service.search_notes(self.conn, self.alice, "alpha", **kw)]
            for hidden in (self.n_other_priv, self.n_other_priv_arch, self.n_eve, self.n_eve_arch):
                self.assertNotIn(hidden, ids)
        ids = [r["id"] for r in service.search_notes(self.conn, self.bob, "alpha")]
        self.assertIn(self.n_other_priv, ids)
        self.assertNotIn(self.n_other_priv_arch, ids)

    def test_v8_others_hidden_even_with_include_archived(self):
        _, total = self.ids(include_archived=True)
        self.assertEqual(total, 2)

    def test_v10_default_call_unchanged_without_archived(self):
        self.setUp_clean()
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual(set(r), {"items", "total", "page", "per_page"})
        self.assertEqual(set(r["items"][0]), {"id", "title", "updated_at"})
        self.assertEqual(r["total"], 2)
        self.assertEqual([i["title"] for i in service.list_notes(self.conn, self.alice, sort="title")["items"]], ["a", "b"])
        self.assertEqual(service.search_notes(self.conn, self.alice, "a")[0].keys(), {"id", "title"})

    def setUp_clean(self):
        self.conn.execute("DELETE FROM note_tags")
        self.conn.execute("DELETE FROM notes")
        service.create_note(self.conn, self.alice, "a", "x")
        service.create_note(self.conn, self.alice, "b", "x")

    def test_v10_include_archived_rejects_non_bool(self):
        for bad in ("1 OR 1=1", 0, 1, None, "x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    service.list_notes(self.conn, self.alice, include_archived=bad)
                with self.assertRaises(ValidationError):
                    service.search_notes(self.conn, self.alice, "alpha", include_archived=bad)

    def test_v11_get_archived_note(self):
        before = service.get_note(self.conn, self.alice, self.n_live)
        n = service.get_note(self.conn, self.alice, self.n_arch)
        self.assertEqual(n["archived"], 1)
        self.assertEqual(n["views"], 1)
        self.assertEqual(service.get_note(self.conn, self.alice, self.n_arch)["views"], 2)
        self.assertEqual(before["archived"], 0)
        for k in ("id", "org_id", "owner_id", "title", "body", "private", "version", "views", "updated_at", "tags"):
            self.assertIn(k, n)
        self.assertEqual(n["tags"], ["t1"])


class ArchiveLogTest(Base):
    def test_v17_log_and_errors_hide_body_and_key(self):
        secret = "SECRET-BODY-TEXT"
        nid = service.create_note(self.conn, self.alice, "title-x", secret)
        with self.assertLogs("notes", level="DEBUG") as cm:
            service.archive_note(self.conn, self.alice, nid)
            service.unarchive_note(self.conn, self.alice, nid)
        text = "\n".join(cm.output)
        self.assertIn(f"id={nid}", text)
        for s in (secret, "title-x", self.alice["api_key"]):
            self.assertNotIn(s, text)
        service.archive_note(self.conn, self.alice, nid)
        errs = []
        for fn, u in ((service.archive_note, self.alice), (service.unarchive_note, self.bob)):
            try:
                fn(self.conn, u, nid)
            except Exception as e:  # noqa: BLE001
                errs.append(str(e))
        try:
            service.update_note(self.conn, self.alice, nid, 99, body="y")
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))
        self.assertEqual(len(errs), 3)
        for m in errs:
            for s in (secret, "title-x", self.alice["api_key"]):
                self.assertNotIn(s, m)
