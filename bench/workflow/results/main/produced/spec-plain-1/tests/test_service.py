import logging
import os
import tempfile
import threading
import unittest

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.path)
        self.alice = db.add_user(self.conn, 1, 1, "alice", "member", "alicekey-12345678")
        self.bob = db.add_user(self.conn, 2, 1, "bob", "member", "bobkey")
        self.root = db.add_user(self.conn, 3, 1, "root", "admin", "rootkey-abcdefgh")
        self.eve = db.add_user(self.conn, 4, 2, "eve", "member", "evekey-zzzzzzzz")
        self.eadmin = db.add_user(self.conn, 5, 2, "eadmin", "admin", "eadminkey-yyyyyyy")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.addCleanup(setattr, service.hooks, "before_write", service.hooks.before_write)

    def count(self, table):
        return self.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

    def new(self, user=None, **kw):
        return service.create_note(self.conn, user or self.alice, kw.pop("title", "title"),
                                   kw.pop("body", "body"), **kw)


class CreateTests(Base):
    def test_create_and_tags_dedup(self):
        nid = self.new(tags=["a", "b", "a"])
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(n["tags"], ["a", "b"])
        self.assertEqual(n["version"], 1)
        self.assertEqual(n["owner_id"], 1)

    def test_rejects(self):
        bad = [dict(title=""), dict(title="   "), dict(title="x" * 101), dict(title=None),
               dict(body="x" * 10001), dict(body=None),
               dict(tags=["a"] * 6), dict(tags="abc"), dict(tags=["A"]), dict(tags=[""]),
               dict(tags=["x" * 21]), dict(tags=["a_b"]), dict(tags=[1]), dict(tags=["a\n"])]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                self.new(**kw)
        self.assertEqual(self.count("notes"), 0)
        self.assertEqual(self.count("audit"), 0)

    def test_limits_ok(self):
        self.new(title="x" * 100, body="y" * 10000, tags=["a-1", "b", "c", "d", "x" * 20])

    def test_failure_rolls_back(self):
        def boom(step):
            raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            self.new(tags=["a"])
        self.assertEqual(self.count("notes"), 0)
        self.assertEqual(self.count("audit"), 0)
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = lambda step: None
        self.assertIsInstance(self.new(), int)

    def test_audit_and_log(self):
        with self.assertLogs("notes", "INFO") as cm:
            nid = self.new(body="SECRET-BODY")
        self.assertNotIn("SECRET-BODY", "".join(cm.output))
        self.assertIn(str(nid), "".join(cm.output))
        a = service.export_audit(self.conn, self.root)
        self.assertEqual((a[0]["action"], a[0]["detail"]), ("create", "note=%d" % nid))


class GetTests(Base):
    def test_visibility(self):
        pub = self.new()
        priv = self.new(private=True)
        self.assertEqual(service.get_note(self.conn, self.bob, pub)["id"], pub)
        for u, n in [(self.bob, priv), (self.root, priv), (self.eve, pub), (self.alice, 999)]:
            with self.assertRaises(NotFound):
                service.get_note(self.conn, u, n)
        self.assertEqual(service.get_note(self.conn, self.alice, priv)["id"], priv)

    def test_views_increment(self):
        nid = self.new()
        self.assertEqual([service.get_note(self.conn, self.bob, nid)["views"] for _ in range(3)],
                         [1, 2, 3])

    def test_views_concurrent(self):
        nid = self.new()
        n_threads, per = 8, 10
        errors = []

        def work():
            c = db.connect(self.path)
            try:
                for _ in range(per):
                    service.get_note(c, self.bob, nid)
            except Exception as e:
                errors.append(e)
            finally:
                c.close()
        ts = [threading.Thread(target=work) for _ in range(n_threads)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], n_threads * per + 1)

    def test_deleted_between_check_and_view(self):
        nid = self.new()
        service.hooks.before_write = lambda s: (
            service.delete_note(self.conn, self.alice, nid) if s == "view" else None)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)
        self.assertFalse(self.conn.in_transaction)


class ListSearchTests(Base):
    def test_list(self):
        a = self.new(title="b")
        b = self.new(title="a")
        self.new(user=self.alice, title="hidden", private=True)
        self.new(user=self.eve, title="other")
        r = service.list_notes(self.conn, self.bob)
        self.assertEqual(r["total"], 2)
        self.assertEqual([i["id"] for i in r["items"]], [b, a])  # 同時刻なら id の大きい順
        r = service.list_notes(self.conn, self.bob, sort="title")
        self.assertEqual([i["title"] for i in r["items"]], ["a", "b"])
        self.assertEqual(service.list_notes(self.conn, self.alice)["total"], 3)
        r = service.list_notes(self.conn, self.bob, page=2, per_page=1)
        self.assertEqual((len(r["items"]), r["page"], r["per_page"]), (1, 2, 1))

    def test_list_rejects(self):
        for kw in [dict(page=0), dict(page=-1), dict(per_page=0), dict(per_page=51),
                   dict(sort="id"), dict(sort="title; DROP TABLE notes"), dict(page="1"),
                   dict(per_page=True)]:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **kw)

    def test_search(self):
        a = self.new(title="100% sure", body="x", tags=["t1"])
        b = self.new(title="a_b", body="zzz", tags=["t2"])
        c = self.new(title="plain", body="has 100% inside")
        self.new(title="secret 100%", private=True, user=self.bob)
        self.new(title="100% other org", user=self.eve)
        s = lambda q, **k: [r["id"] for r in service.search_notes(self.conn, self.alice, q, **k)]
        self.assertEqual(s("100%"), [a, c])
        self.assertEqual(s("a_b"), [b])
        self.assertEqual(s("a%b"), [])
        self.assertEqual(s("_"), [b])
        self.assertEqual(s("100%", tags=["t1", "t2"]), [a])
        self.assertEqual(s("a_b", tags=["t1", "t2"]), [b])
        self.assertEqual(s("100%", tags=["t2"]), [])
        self.assertEqual(s("'; DROP TABLE notes; --"), [])
        self.assertEqual(self.count("notes"), 5)

    def test_search_rejects(self):
        for q, t in [("", ()), ("  ", ()), ("x" * 101, ()), (None, ()), ("a", ["A"]),
                     ("a", "abc"), ("a", ["a"] * 6)]:
            with self.subTest(q=q, t=t), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, q, tags=t)


class UpdateDeleteTests(Base):
    def test_update(self):
        nid = self.new(tags=["a"])
        self.assertEqual(service.update_note(self.conn, self.alice, nid, 1, title="new"), 2)
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("new", "body", ["a"], 2))
        service.update_note(self.conn, self.root, nid, 2, tags=["x", "y"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["x", "y"])

    def test_permissions(self):
        nid = self.new()
        priv = self.new(private=True)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, nid)
        for u in (self.root, self.eve, self.eadmin, self.bob):
            with self.assertRaises(NotFound):
                service.update_note(self.conn, u, priv, 1, title="x")
            with self.assertRaises(NotFound):
                service.delete_note(self.conn, u, priv)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, 999, 1, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.eadmin, nid, 1, title="x")

    def test_validation(self):
        nid = self.new()
        for kw in [dict(title=""), dict(body="x" * 10001), dict(tags=["BAD"]), dict(tags="abc")]:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, nid, 1, **kw)
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["version"], 1)

    def test_stale_version(self):
        nid = self.new()
        service.update_note(self.conn, self.alice, nid, 1, title="a")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, title="b")

    def test_concurrent_update_conflict(self):
        nid = self.new()

        def other(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.update_note(self.conn, self.root, nid, 1, title="first")
        service.hooks.before_write = other
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, title="second")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["title"], "first")
        self.assertFalse(self.conn.in_transaction)

    def test_delete(self):
        nid = self.new(tags=["a"])
        service.delete_note(self.conn, self.root, nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (0, 0))
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)

    def test_delete_failure_rolls_back(self):
        nid = self.new(tags=["a"])

        def boom(step):
            if step == "delete:note":
                raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (1, 1))
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = lambda s: None
        service.delete_note(self.conn, self.alice, nid)


class TransferAuditTests(Base):
    def test_transfer(self):
        a = self.new()
        b = self.new(private=True)
        self.new(user=self.bob)
        n = service.transfer_notes(self.conn, self.root, self.alice, self.bob)
        self.assertEqual(n, 2)
        rows = self.conn.execute("SELECT owner_id, version FROM notes WHERE id IN (?, ?)", (a, b)).fetchall()
        self.assertTrue(all((r[0], r[1]) == (2, 2) for r in rows))
        last = service.export_audit(self.conn, self.root)[-1]
        self.assertEqual((last["action"], last["detail"]), ("transfer", "from=1 to=2 count=2"))

    def test_forbidden(self):
        self.new()
        for adm, f, t in [(self.alice, self.alice, self.bob), (self.root, self.alice, self.eve),
                          (self.root, self.eve, self.alice), (self.eadmin, self.alice, self.bob)]:
            with self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, adm, f, t)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes WHERE owner_id=1").fetchone()[0], 1)

    def test_all_or_nothing(self):
        ids = [self.new() for _ in range(3)]

        def boom(step):
            if step == "transfer:%d" % ids[2]:
                raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.root, self.alice, self.bob)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE owner_id=1 AND version=1").fetchone()[0], 3)
        self.assertEqual(self.count("audit"), 3)
        self.assertFalse(self.conn.in_transaction)

    def test_export_audit(self):
        self.new()
        self.new(user=self.bob)
        self.new(user=self.eve)
        out = service.export_audit(self.conn, self.root)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["actor"], "alice")
        self.assertEqual(out[0]["actor_key"], "*" * 13 + "5678")
        self.assertEqual(out[1]["actor_key"], "*" * 6)
        self.assertNotIn("alicekey", repr(out))
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)


if __name__ == "__main__":
    unittest.main()
