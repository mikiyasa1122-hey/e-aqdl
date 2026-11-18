"""AC21・K15: 引数なしの list/search/get の戻り値の形（archived の追加以外は従来どおり）。"""
from notes import service

from .base import Base

GET_KEYS_BEFORE = {"id", "org_id", "owner_id", "title", "body", "private", "version", "views", "updated_at", "tags"}


class ShapeTest(Base):
    def setUp(self):
        super().setUp()
        self.n1 = service.create_note(self.conn, self.alice, "hello", "world", ["a"])
        self.n2 = service.create_note(self.conn, self.alice, "second", "hello again", private=True)

    def test_get_note_keys_only_add_archived(self):
        n = service.get_note(self.conn, self.alice, self.n1)
        self.assertEqual(set(n), GET_KEYS_BEFORE | {"archived"})
        self.assertEqual({k: n[k] for k in ("title", "body", "private", "version", "views", "tags", "archived")},
                         {"title": "hello", "body": "world", "private": 0, "version": 1, "views": 1, "tags": ["a"],
                          "archived": 0})

    def test_list_notes_shape_and_values(self):
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual(set(r), {"items", "total", "page", "per_page"})
        self.assertEqual((r["total"], r["page"], r["per_page"]), (2, 1, 20))
        for item in r["items"]:
            self.assertEqual(set(item), {"id", "title", "updated_at"})
        self.assertEqual(len(service.list_notes(self.conn, self.bob)["items"]), 1)

    def test_search_notes_shape_and_values(self):
        r = service.search_notes(self.conn, self.alice, "hello")
        self.assertEqual(r, [{"id": self.n1, "title": "hello"}, {"id": self.n2, "title": "second"}])
        self.assertEqual(service.search_notes(self.conn, self.alice, "hello", ["a"]), [{"id": self.n1, "title": "hello"}])

    def test_positional_arguments_still_work(self):
        r = service.list_notes(self.conn, self.alice, 1, 1, "title")
        self.assertEqual((len(r["items"]), r["total"]), (1, 2))
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "hello", ("a",))), 1)
