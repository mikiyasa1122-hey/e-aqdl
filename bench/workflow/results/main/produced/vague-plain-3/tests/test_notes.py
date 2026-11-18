import os
import tempfile
import threading
import unittest

from notes import db, service, share
from notes.errors import Conflict, Forbidden, NotFound, RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.connect(self.path)
        self.alice = db.add_user(self.conn, 1, 1, "alice", api_key="secretkey-ALICE1234")
        self.bob = db.add_user(self.conn, 2, 1, "bob", api_key="secretkey-BOB56789")
        self.adm = db.add_user(self.conn, 3, 1, "adm", "admin", api_key="secretkey-ADMIN000")
        self.eve = db.add_user(self.conn, 4, 2, "eve", "admin", api_key="secretkey-EVE00000")
        self.addCleanup(setattr, service.hooks, "before_write", service.hooks.before_write)
        service.hooks.before_write = lambda step: None


class NoteTests(Base):
    def test_create_get_tags_views(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["z", "a", "a"])
        n = service.get_note(self.conn, self.bob, nid)
        self.assertEqual((n["tags"], n["views"], n["version"]), (["a", "z"], 1, 1))
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], 2)

    def test_org_isolation_and_private(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        p = service.create_note(self.conn, self.alice, "p", "b", private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.eve, n)
        for u in (self.bob, self.adm):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, u, p)
        self.assertEqual(service.list_notes(self.conn, self.bob)["total"], 1)
        self.assertEqual(service.search_notes(self.conn, self.bob, ""), [{"id": n, "title": "t"}])
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "")), 2)
        self.assertEqual(service.list_notes(self.conn, self.eve)["total"], 0)

    def test_list_sort_paging(self):
        ids = [service.create_note(self.conn, self.alice, t, "b") for t in "cab"]
        r = service.list_notes(self.conn, self.alice, sort="title", page=1, per_page=2)
        self.assertEqual([i["title"] for i in r["items"]], ["a", "b"])
        self.assertEqual(r["total"], 3)
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual([i["id"] for i in r["items"]], ids[::-1])
        for bad in ({"page": 0}, {"per_page": 0}, {"per_page": 1000}, {"sort": "x"}):
            with self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **bad)

    def test_search(self):
        a = service.create_note(self.conn, self.alice, "Hello", "world 100%", ["x", "y"])
        b = service.create_note(self.conn, self.alice, "other", "100 items", ["x"])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.bob, "100%")], [a])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.bob, "", ["x"])], [a, b])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.bob, "", ["x", "y"])], [a])
        self.assertEqual(service.search_notes(self.conn, self.bob, "_"), [])

    def test_validation(self):
        for args in (("", "b"), (None, "b"), ("t", 5), ("t", "b", "abc"), ("t", "b", [""]), ("t", "b", [1])):
            with self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, *args)

    def test_update_permissions_and_conflict(self):
        n = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, n, 1, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.eve, n, 1, title="x")
        self.assertEqual(service.update_note(self.conn, self.adm, n, 1, title="x"), 2)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, n, 1, body="y")
        self.assertEqual(service.update_note(self.conn, self.alice, n, 2, tags=["q"]), 3)
        g = service.get_note(self.conn, self.alice, n)
        self.assertEqual((g["title"], g["body"], g["tags"]), ("x", "b", ["q"]))

    def test_concurrent_update_does_not_lose_change(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        other = db.connect(self.path)

        def interfere(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.update_note(other, self.alice, n, 1, title="theirs")
        service.hooks.before_write = interfere
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, n, 1, title="mine")
        self.assertEqual(service.get_note(self.conn, self.alice, n)["title"], "theirs")

    def test_delete(self):
        n = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, n)
        service.delete_note(self.conn, self.adm, n)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, n)

    def test_failures_roll_back(self):
        def boom(step):
            raise RuntimeError(step)
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", ["a"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        service.hooks.before_write = lambda s: None
        n = service.create_note(self.conn, self.alice, "t", "b", ["a"])
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, n)
        service.hooks.before_write = lambda s: None
        self.assertEqual(service.get_note(self.conn, self.alice, n)["tags"], ["a"])

    def test_view_deleted_in_between(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        service.hooks.before_write = lambda s: service.delete_note(db.connect(self.path), self.alice, n) \
            if s == "view" else None
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, n)

    def test_concurrent_views(self):
        n = service.create_note(self.conn, self.alice, "t", "b")

        def work():
            c = db.connect(self.path)
            for _ in range(10):
                service.get_note(c, self.bob, n)
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(service.get_note(self.conn, self.bob, n)["views"], 41)

    def test_logging_no_body(self):
        with self.assertLogs("notes", "INFO") as cm:
            n = service.create_note(self.conn, self.alice, "t", "SECRETBODY")
        self.assertIn(str(n), cm.output[0])
        self.assertNotIn("SECRETBODY", cm.output[0])


class AdminTests(Base):
    def test_transfer(self):
        a = service.create_note(self.conn, self.alice, "t", "b")
        p = service.create_note(self.conn, self.alice, "p", "b", private=True)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.bob, self.alice, self.bob)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.adm, self.alice, self.eve)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.eve, self.alice, self.bob)
        self.assertEqual(service.transfer_notes(self.conn, self.adm, self.alice, self.bob), 2)
        self.assertEqual(service.get_note(self.conn, self.bob, p)["version"], 2)
        self.assertEqual(service.update_note(self.conn, self.bob, a, 2, title="n"), 3)
        self.assertIn({"id": 3, "action": "transfer", "detail": "from=1 to=2 count=2"},
                      [{k: r[k] for k in ("id", "action", "detail")} for r in
                       service.export_audit(self.conn, self.adm)])

    def test_transfer_atomic(self):
        for t in "ab":
            service.create_note(self.conn, self.alice, t, "b")

        def boom(step):
            if step == "transfer:2":
                raise RuntimeError
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.adm, self.alice, self.bob)
        self.assertEqual(service.list_notes(self.conn, self.alice)["total"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes WHERE owner_id=2").fetchone()[0], 0)

    def test_audit_masks_keys(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        service.delete_note(self.conn, self.alice, n)
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)
        log = service.export_audit(self.conn, self.adm)
        self.assertEqual([r["action"] for r in log], ["create", "delete"])
        self.assertEqual(log[0]["actor"], "alice")
        self.assertNotIn("secretkey", repr(log))
        self.assertEqual(service.export_audit(self.conn, self.eve), [])


class RateLimitTests(unittest.TestCase):
    def test_limit_and_window(self):
        t = [0.0]
        rl = RateLimiter(2, 10, clock=lambda: t[0])
        self.assertEqual(rl.hit(1), 1)
        self.assertEqual(rl.hit(1), 0)
        with self.assertRaises(RateLimited):
            rl.hit(1)
        self.assertEqual(rl.hit(2), 1)
        t[0] = 10.0
        self.assertEqual(rl.hit(1), 1)

    def test_race_in_hook(self):
        rl = RateLimiter(1, 60)
        calls = []

        def hook():
            if not calls:
                calls.append(1)
                rl.hit("u")
        rl.hook = hook
        with self.assertRaises(RateLimited):
            rl.hit("u")

    def test_threads(self):
        rl = RateLimiter(5, 60)
        ok = []

        def w():
            try:
                rl.hit("u")
                ok.append(1)
            except RateLimited:
                pass
        ts = [threading.Thread(target=w) for _ in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(ok), 5)


class ShareTests(unittest.TestCase):
    def test_roundtrip_and_format(self):
        link = share.make_link("s", 7, 100, now=1000)
        self.assertTrue(link.startswith("7.1100."))
        self.assertEqual(share.verify_link("s", link, now=1100), 7)

    def test_invalid(self):
        link = share.make_link("s", 7, 100, now=1000)
        nid, exp, sig = link.split(".")
        for bad in (link, f"8.{exp}.{sig}", f"{nid}.{int(exp) + 1}.{sig}", f"{nid}.{exp}.{sig[:-1]}",
                    "", "a.b.c", "1.2", None, f"{nid}.{exp}.{sig}.x"):
            with self.assertRaises(Forbidden):
                share.verify_link("s" if bad != link else "other", bad, now=1000)
        with self.assertRaises(Forbidden):
            share.verify_link("s", link, now=1101)
        with self.assertRaises(ValidationError):
            share.make_link("s", 1, ttl_sec=0)


if __name__ == "__main__":
    unittest.main()
