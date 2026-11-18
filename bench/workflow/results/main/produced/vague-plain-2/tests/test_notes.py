import threading
import unittest

from notes import db, service, share
from notes.errors import Conflict, Forbidden, NotFound, RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.alice = db.add_user(self.conn, 1, 1, "alice", "member", "alice-secret-key-1234")
        self.bob = db.add_user(self.conn, 2, 1, "bob", "member", "bob-secret-key-5678")
        self.root = db.add_user(self.conn, 3, 1, "root", "admin", "root-secret-key-9999")
        self.eve = db.add_user(self.conn, 4, 2, "eve", "member", "eve-secret-key-0000")
        self.addCleanup(setattr, service.hooks, "before_write", service.hooks.before_write)
        self.addCleanup(setattr, service, "rate_limiter", None)


class NoteTests(Base):
    def test_create_get_views_tags(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["y", "x", "x"])
        n = service.get_note(self.conn, self.bob, nid)
        self.assertEqual((n["tags"], n["views"], n["version"]), (["x", "y"], 1, 1))
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], 2)

    def test_isolation_and_private(self):
        pub = service.create_note(self.conn, self.alice, "pub", "b")
        priv = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.eve, pub)
        for u in (self.bob, self.root):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, u, priv)
            self.assertEqual([i["id"] for i in service.list_notes(self.conn, u)["items"]], [pub])
            self.assertEqual(service.search_notes(self.conn, u, "")[0]["id"], pub)
        self.assertEqual(service.list_notes(self.conn, self.alice)["total"], 2)
        self.assertEqual(service.list_notes(self.conn, self.eve)["total"], 0)

    def test_list_sort_paging_and_search(self):
        a = service.create_note(self.conn, self.alice, "b", "x%y", ["t1"])
        b = service.create_note(self.conn, self.alice, "a", "zzz", ["t1", "t2"])
        c = service.create_note(self.conn, self.alice, "a", "xay", ["t2"])
        r = service.list_notes(self.conn, self.alice, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [b, c, a])
        r = service.list_notes(self.conn, self.alice, page=2, per_page=2, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [a])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "x%y")], [a])
        self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "", ["t1", "t2"])], [b])
        with self.assertRaises(ValidationError):
            service.list_notes(self.conn, self.alice, sort="bogus")
        with self.assertRaises(ValidationError):
            service.list_notes(self.conn, self.alice, page=0)

    def test_update_permissions_and_conflict(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 1, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.eve, nid, 1, title="x")
        self.assertEqual(service.update_note(self.conn, self.root, nid, 1, title="x"), 2)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, body="stale")
        service.update_note(self.conn, self.alice, nid, 2, tags=[])
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("x", "b", [], 3))

    def test_concurrent_update_does_not_lose_change(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def rival(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.update_note(self.conn, self.alice, nid, 1, title="theirs")
        service.hooks.before_write = rival
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.root, nid, 1, title="mine")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["title"], "theirs")

    def test_update_after_delete_is_not_found(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def rival(step):
            service.hooks.before_write = lambda s: None
            service.delete_note(self.conn, self.alice, nid)
        service.hooks.before_write = rival
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, nid, 1, title="x")

    def test_delete_rollback_on_failure(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])

        def boom(step):
            raise RuntimeError(step)
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        service.hooks.before_write = lambda s: None
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["a"])
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, nid)
        service.delete_note(self.conn, self.root, nid)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)

    def test_create_rollback_on_failure(self):
        def boom(step):
            raise RuntimeError(step)
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", ["a"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)

    def test_view_of_deleted_note(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def rival(step):
            service.hooks.before_write = lambda s: None
            service.delete_note(self.conn, self.alice, nid)
        service.hooks.before_write = rival
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)

    def test_validation(self):
        for kw in ({"title": ""}, {"title": 5}, {"body": None}, {"tags": "abc"}, {"tags": [""]}):
            args = dict(title="t", body="b")
            args.update(kw)
            with self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, **args)

    def test_threads_views_not_lost(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "n.db")
            c0 = db.connect(path)
            db.add_user(c0, 1, 1, "a")
            nid = service.create_note(c0, {"id": 1, "org_id": 1, "role": "member"}, "t", "b")

            def work():
                c = db.connect(path)
                for _ in range(10):
                    service.get_note(c, {"id": 1, "org_id": 1, "role": "member"}, nid)
            ts = [threading.Thread(target=work) for _ in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(service.get_note(c0, {"id": 1, "org_id": 1, "role": "member"}, nid)["views"], 41)


class TransferAuditTests(Base):
    def test_transfer(self):
        a = service.create_note(self.conn, self.alice, "t", "b", private=True)
        b = service.create_note(self.conn, self.alice, "t2", "b")
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.bob, 1, 2)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.root, 1, 4)
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.root, 1, 1)
        self.assertEqual(service.transfer_notes(self.conn, self.root, 1, 2), 2)
        n = service.get_note(self.conn, self.bob, a)
        self.assertEqual((n["owner_id"], n["version"]), (2, 2))
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, a)
        self.assertEqual(service.get_note(self.conn, self.bob, b)["version"], 2)

    def test_transfer_is_atomic(self):
        a = service.create_note(self.conn, self.alice, "t", "b")
        b = service.create_note(self.conn, self.alice, "t2", "b")

        def boom(step):
            if step == "transfer:%d" % b:
                raise RuntimeError
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.root, 1, 2)
        service.hooks.before_write = lambda s: None
        self.assertEqual(service.get_note(self.conn, self.alice, a)["owner_id"], 1)

    def test_audit_masks_keys_and_scopes_org(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.create_note(self.conn, self.eve, "t", "b")
        service.update_note(self.conn, self.alice, nid, 1, title="x")
        service.delete_note(self.conn, self.alice, nid)
        service.transfer_notes(self.conn, self.root, 1, 2)
        log = service.export_audit(self.conn, self.root)
        self.assertEqual([(r["action"], r["detail"]) for r in log],
                         [("create", "note=1"), ("update", "note=1"), ("delete", "note=1"),
                          ("transfer", "from=1 to=2 count=0")])
        self.assertEqual(log[0]["actor"], "alice")
        self.assertNotIn("secret", repr(log))
        self.assertEqual(log[0]["actor_key"], "****1234")
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)

    def test_log_has_no_body(self):
        with self.assertLogs("notes", "INFO") as cm:
            service.create_note(self.conn, self.alice, "t", "SECRETBODY")
        self.assertNotIn("SECRETBODY", "".join(cm.output))
        self.assertIn("id=1", cm.output[0])


class RateLimitTests(unittest.TestCase):
    def test_limit_and_window(self):
        t = [0.0]
        rl = RateLimiter(2, 10, clock=lambda: t[0])
        self.assertEqual(rl.hit(1), 1)
        self.assertEqual(rl.hit(1), 0)
        with self.assertRaises(RateLimited):
            rl.hit(1)
        self.assertEqual(rl.hit(2), 1)
        t[0] = 10
        self.assertEqual(rl.hit(1), 1)

    def test_concurrent_hits_cannot_exceed_limit(self):
        rl = RateLimiter(1, 60)
        calls = []

        def hook():
            if not calls:
                calls.append(1)
                rl.hit(1)  # 同時の呼び出し
        rl.hook = hook
        with self.assertRaises(RateLimited):
            rl.hit(1)

    def test_threads(self):
        rl = RateLimiter(50, 60)
        ok = []

        def w():
            for _ in range(20):
                try:
                    rl.hit(1)
                    ok.append(1)
                except RateLimited:
                    pass
        ts = [threading.Thread(target=w) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(ok), 50)

    def test_service_integration(self):
        c = db.connect()
        u = db.add_user(c, 1, 1, "a")
        service.rate_limiter = RateLimiter(1, 60)
        try:
            service.create_note(c, u, "t", "b")
            with self.assertRaises(RateLimited):
                service.create_note(c, u, "t", "b")
        finally:
            service.rate_limiter = None


class ShareTests(unittest.TestCase):
    def test_roundtrip_and_expiry(self):
        link = share.make_link("s", 7, 100, now=1000)
        self.assertTrue(link.startswith("7.1100."))
        self.assertEqual(share.verify_link("s", link, now=1100), 7)
        with self.assertRaises(Forbidden):
            share.verify_link("s", link, now=1101)

    def test_tamper(self):
        link = share.make_link("s", 7, 100, now=1000)
        n, e, sig = link.split(".")
        for bad in ("8.%s.%s" % (e, sig), "%s.9999.%s" % (n, sig), link + "0", "", "a.b.c", "x",
                    "%s.%s.%s" % (n, e, sig.upper()), None):
            with self.assertRaises(Forbidden):
                share.verify_link("s", bad, now=1000)
        with self.assertRaises(Forbidden):
            share.verify_link("other", link, now=1000)


if __name__ == "__main__":
    unittest.main()
