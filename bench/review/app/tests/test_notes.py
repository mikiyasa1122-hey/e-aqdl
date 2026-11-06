import unittest

from notes import db, service, share
from notes.errors import Forbidden, NotFound, RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.alice = db.add_user(self.conn, 1, 10, "alice", api_key="ak-alice-0001")
        self.bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        self.admin = db.add_user(self.conn, 3, 10, "root", role="admin", api_key="ak-root-0003")


class NotesTest(Base):
    def test_create_and_get(self):
        nid = service.create_note(self.conn, self.alice, "買い物", "牛乳", ["home"])
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("買い物", "牛乳", ["home"], 1))

    def test_empty_title_rejected(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "", "x")

    def test_list_first_page(self):
        for i in range(3):
            service.create_note(self.conn, self.alice, f"t{i}", "b")
        res = service.list_notes(self.conn, self.bob, page=1, per_page=2)
        self.assertEqual(res["total"], 3)
        self.assertEqual(len(res["items"]), 2)

    def test_search(self):
        service.create_note(self.conn, self.alice, "議事録", "予算の話")
        service.create_note(self.conn, self.alice, "メモ", "別の話")
        self.assertEqual([r["title"] for r in service.search_notes(self.conn, self.bob, "予算")], ["議事録"])

    def test_update_by_owner(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        v = service.update_note(self.conn, self.alice, nid, 1, body="c")
        self.assertEqual(v, 2)
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["body"], "c")

    def test_delete_by_owner(self):
        nid = service.create_note(self.conn, self.alice, "a", "b", ["x"])
        service.delete_note(self.conn, self.alice, nid)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)

    def test_transfer(self):
        service.create_note(self.conn, self.alice, "a", "b")
        service.create_note(self.conn, self.alice, "c", "d")
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 2)

    def test_transfer_requires_admin(self):
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.alice, self.alice, self.bob)

    def test_export_audit(self):
        service.create_note(self.conn, self.alice, "a", "b")
        rows = service.export_audit(self.conn, self.admin)
        self.assertEqual(rows[0]["action"], "create")
        self.assertEqual(rows[0]["actor"], "alice")


class RateLimitTest(unittest.TestCase):
    def test_limit(self):
        t = [0.0]
        rl = RateLimiter(limit=2, window_sec=60, clock=lambda: t[0])
        rl.hit(1)
        rl.hit(1)
        with self.assertRaises(RateLimited):
            rl.hit(1)
        t[0] = 61
        rl.hit(1)


class ShareTest(unittest.TestCase):
    def test_roundtrip(self):
        link = share.make_link("s3cret", 7, ttl_sec=60, now=1000)
        self.assertEqual(share.verify_link("s3cret", link, now=1010), 7)

    def test_tampered(self):
        link = share.make_link("s3cret", 7, ttl_sec=60, now=1000)
        with self.assertRaises(Forbidden):
            share.verify_link("s3cret", link.replace("7.", "8.", 1), now=1010)


if __name__ == "__main__":
    unittest.main()
