"""AC1・K4: R1 の規則どおりに作成・拒否され、失敗したら何も残らない。"""
from notes import service
from notes.errors import ValidationError

from .base import Base


class CreateTest(Base):
    def test_create_and_read_back(self):
        nid = service.create_note(self.conn, self.alice, "買い物", "牛乳", ["home", "todo"], private=True)
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["private"], n["version"], n["owner_id"], n["org_id"]),
                         ("買い物", "牛乳", ["home", "todo"], 1, 1, 1, 10))

    def test_title_boundaries(self):
        service.create_note(self.conn, self.alice, "a", "")
        service.create_note(self.conn, self.alice, "a" * 100, "")
        for bad in ("", "   ", "\t\n", "a" * 101, None, 5):
            with self.subTest(title=bad), self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, bad, "x")

    def test_body_boundaries(self):
        service.create_note(self.conn, self.alice, "t", "b" * 10000)
        for bad in ("b" * 10001, None, 1):
            with self.subTest(body=type(bad)), self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, "t", bad)

    def test_tag_rules(self):
        service.create_note(self.conn, self.alice, "t", "b", ["a", "z-9", "x" * 20])
        bad_tags = [["A"], ["a b"], ["x" * 21], [""], ["abc\n"], ["\nabc"], ["日本"], ["a_b"], [1], [None]]
        for tags in bad_tags:
            with self.subTest(tags=tags), self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, "t", "b", tags)

    def test_tag_count_counts_duplicates_but_stores_unique(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["a", "a", "b", "b", "c"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["a", "b", "c"])
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", ["a", "a", "b", "b", "c", "c"])

    def test_tags_must_be_a_sequence_not_a_string(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", "abc")
        service.create_note(self.conn, self.alice, "t", "b", ("a", "b"))

    def test_rejected_input_leaves_nothing(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", ["ok", "BAD"])
        self.assertEqual((self.count("notes"), self.count("note_tags"), self.count("audit")), (0, 0, 0))

    def test_failure_in_the_middle_leaves_nothing(self):
        self.fail_at("create:tags")
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", ["a"])
        self.assertEqual((self.count("notes"), self.count("note_tags"), self.count("audit")), (0, 0, 0))

    def test_audit_recorded_in_same_operation(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        rows = self.conn.execute("SELECT org_id, actor_id, action, detail FROM audit").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(10, 1, "create", f"note={nid}")])
