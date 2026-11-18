import os
import tempfile
import threading
import unittest

from notes import db, service, share
from notes.errors import Conflict, Forbidden, NotFound, RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.conn = db.connect(self.path)
        self.admin = db.add_user(self.conn, 1, 1, "adm", "admin", "secret-key-ABCD1234")
        self.alice = db.add_user(self.conn, 2, 1, "alice", "member", "alice-key-9999999")
        self.bob = db.add_user(self.conn, 3, 1, "bob", "member", "bob")
        self.eve = db.add_user(self.conn, 4, 2, "eve", "admin", "eve-key-00000000")
        self.orig = service.hooks.before_write

    def tearDown(self):
        service.hooks.before_write = self.orig
        self.conn.close()
        self.dir.cleanup()


class NoteTests(Base):
    def test_org_isolation_and_private(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        p = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.eve, n)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, p)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.admin, p)
        self.assertEqual(service.list_notes(self.conn, self.bob)["total"], 1)
        self.assertEqual(service.list_notes(self.conn, self.alice)["total"], 2)
        self.assertEqual(service.search_notes(self.conn, self.bob, "")[0]["id"], n)
        self.assertEqual(service.list_notes(self.conn, self.eve)["total"], 0)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.eve, n)

    def test_get_views_and_tags(self):
        n = service.create_note(self.conn, self.alice, "t", "b", tags=["z", "a", "a"])
        self.assertEqual(service.get_note(self.conn, self.alice, n)["views"], 1)
        d = service.get_note(self.conn, self.bob, n)
        self.assertEqual((d["views"], d["tags"], d["version"]), (2, ["a", "z"], 1))

    def test_list_sort_paging(self):
        ids = [service.create_note(self.conn, self.alice, t, "") for t in ("b", "a", "c")]
        r = service.list_notes(self.conn, self.alice, sort="title")
        self.assertEqual([i["title"] for i in r["items"]], ["a", "b", "c"])
        r = service.list_notes(self.conn, self.alice, page=2, per_page=2)
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["total"], 3)
        self.assertEqual(service.list_notes(self.conn, self.alice)["items"][0]["id"], ids[-1])
        for bad in ({"page": 0}, {"per_page": 0}, {"sort": "x"}, {"per_page": 10**6}):
            with self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **bad)

    def test_search(self):
        a = service.create_note(self.conn, self.alice, "100% sure", "x", tags=["t1", "t2"])
        service.create_note(self.conn, self.alice, "other", "hello", tags=["t1"])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.alice, "%")], [a])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "", tags=["t1"])), 2)
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "", tags=["t1", "t2"])), 1)
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "hell")), 1)

    def test_permissions(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, n, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, n)
        self.assertEqual(service.update_note(self.conn, self.admin, n, 1, title="x"), 2)
        service.delete_note(self.conn, self.admin, n)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, n)

    def test_update_conflict_and_tags(self):
        n = service.create_note(self.conn, self.alice, "t", "b", tags=["a"])
        self.assertEqual(service.update_note(self.conn, self.alice, n, 1, body="c", tags=["q"]), 2)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, n, 1, title="lost")
        d = service.get_note(self.conn, self.alice, n)
        self.assertEqual((d["title"], d["body"], d["tags"], d["version"]), ("t", "c", ["q"], 2))

    def test_concurrent_update_does_not_overwrite(self):
        n = service.create_note(self.conn, self.alice, "t", "b")

        def other(step):
            if step == "update":
                service.hooks.before_write = self.orig
                c2 = db.connect(self.path)
                service.update_note(c2, self.admin, n, 1, title="admin")
                c2.close()
        service.hooks.before_write = other
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, n, 1, title="alice")
        self.assertEqual(service.get_note(self.conn, self.alice, n)["title"], "admin")

    def test_delete_during_update_and_view(self):
        n = service.create_note(self.conn, self.alice, "t", "b")

        def kill(step):
            service.hooks.before_write = self.orig
            c2 = db.connect(self.path)
            service.delete_note(c2, self.alice, n)
            c2.close()
        service.hooks.before_write = kill
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, n, 1, title="x")
        n = service.create_note(self.conn, self.alice, "t", "b")
        service.hooks.before_write = kill
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, n)

    def test_failure_rolls_back(self):
        def boom(step):
            raise RuntimeError(step)
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        service.hooks.before_write = self.orig
        n = service.create_note(self.conn, self.alice, "t", "b", tags=["a"])
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, n)
        service.hooks.before_write = self.orig
        self.assertEqual(service.get_note(self.conn, self.alice, n)["tags"], ["a"])
        self.assertFalse(self.conn.in_transaction)

    def test_validation(self):
        for kw in ({"title": ""}, {"title": 1}, {"body": None}, {"tags": "abc"}, {"tags": [""]},
                   {"private": "yes"}):
            args = dict(title="t", body="b")
            args.update(kw)
            with self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, **args)
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, {"id": 1}, "t", "b")

    def test_transfer(self):
        a = service.create_note(self.conn, self.alice, "t", "b")
        p = service.create_note(self.conn, self.alice, "p", "b", private=True)
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 2)
        self.assertEqual(service.get_note(self.conn, self.bob, a)["version"], 2)
        self.assertEqual(service.get_note(self.conn, self.bob, p)["owner_id"], 3)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.alice, self.bob, self.alice)
        with self.assertRaises(NotFound):  # 他組織の利用者へは移せない
            service.transfer_notes(self.conn, self.admin, self.bob, self.eve)
        with self.assertRaises(NotFound):  # 他組織の管理者は操作できない
            service.transfer_notes(self.conn, self.eve, self.bob, self.alice)

    def test_transfer_failure_atomic(self):
        a = service.create_note(self.conn, self.alice, "t", "b")
        b = service.create_note(self.conn, self.alice, "t", "b")

        def boom(step):
            if step == "transfer:%d" % b:
                raise RuntimeError
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        service.hooks.before_write = self.orig
        self.assertEqual(service.get_note(self.conn, self.alice, a)["owner_id"], 2)

    def test_audit(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        service.update_note(self.conn, self.alice, n, 1, title="x")
        service.delete_note(self.conn, self.alice, n)
        log = service.export_audit(self.conn, self.admin)
        self.assertEqual([(r["action"], r["detail"]) for r in log],
                         [("create", "note=%d" % n), ("update", "note=%d" % n), ("delete", "note=%d" % n)])
        self.assertEqual(log[0]["actor"], "alice")
        self.assertNotIn("alice-key-9999999", str(log))
        self.assertTrue(log[0]["actor_key"].endswith("9999"))
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)
        self.assertEqual(service.export_audit(self.conn, self.eve), [])

    def test_logging_no_body(self):
        with self.assertLogs("notes", "INFO") as cm:
            n = service.create_note(self.conn, self.alice, "title", "SECRETBODY")
        self.assertIn("id=%d" % n, cm.output[0])
        self.assertNotIn("SECRETBODY", cm.output[0])


class RateTests(unittest.TestCase):
    def test_limit_and_window(self):
        t = [0]
        r = RateLimiter(limit=2, window_sec=10, clock=lambda: t[0])
        self.assertEqual(r.hit(1), 1)
        self.assertEqual(r.hit(1), 0)
        with self.assertRaises(RateLimited):
            r.hit(1)
        self.assertEqual(r.hit(2), 1)
        t[0] = 10
        self.assertEqual(r.hit(1), 1)

    def test_concurrent_hit_in_hook(self):
        r = RateLimiter(limit=1, window_sec=10, clock=lambda: 0)
        calls = []

        def hook():
            if not calls:
                calls.append(1)
                th = threading.Thread(target=r.hit, args=(1,))
                th.start()
                th.join()
        r.hook = hook
        with self.assertRaises(RateLimited):
            r.hit(1)

    def test_bad_args(self):
        with self.assertRaises(ValidationError):
            RateLimiter(limit=0)


class ShareTests(unittest.TestCase):
    def test_roundtrip(self):
        link = share.make_link("s", 7, ttl_sec=100, now=1000)
        self.assertTrue(link.startswith("7.1100."))
        self.assertEqual(share.verify_link("s", link, now=1100), 7)
        with self.assertRaises(Forbidden):
            share.verify_link("s", link, now=1101)

    def test_tamper(self):
        link = share.make_link("s", 7, now=1000)
        n, e, sig = link.split(".")
        for bad in ("8.%s.%s" % (e, sig), "%s.%d.%s" % (n, int(e) + 1, sig), link + "0",
                    "%s.%s.%s" % (n, e, sig.upper()), "", "a.b.c", None, "1.2"):
            with self.assertRaises(Forbidden):
                share.verify_link("s", bad, now=1000)
        with self.assertRaises(Forbidden):
            share.verify_link("other", link, now=1000)


if __name__ == "__main__":
    unittest.main()
