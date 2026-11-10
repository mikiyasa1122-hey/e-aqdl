import logging
import os
import tempfile
import threading
import unittest

from notes import db, service
from notes.errors import (Conflict, Forbidden, NotFound, RateLimited, ValidationError)
from notes.ratelimit import RateLimiter
from notes.share import make_link, verify_link


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.conn = db.connect(self.path)
        c = self.conn
        self.alice = db.add_user(c, 1, 1, "alice", "member", "abcdefgh12345678")
        self.bob = db.add_user(c, 2, 1, "bob", "member", "short")
        self.adm = db.add_user(c, 3, 1, "adm", "admin", "adminkey-0000")
        self.eve = db.add_user(c, 4, 2, "eve", "member", "evekey")
        self.eadm = db.add_user(c, 5, 2, "eadm", "admin", "eadmkey-99999")
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.conn.close)

    def reset_hook(self):
        service.hooks.__dict__.pop("before_write", None)

    def tearDown(self):
        self.reset_hook()

    def counts(self):
        return [self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("notes", "note_tags", "audit")]

    def new(self, user=None, **kw):
        return service.create_note(self.conn, user or self.alice, kw.pop("title", "t"),
                                   kw.pop("body", "b"), **kw)


class CreateTests(Base):
    def test_create_and_get(self):
        nid = self.new(tags=["b", "a", "a"])
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(n["tags"], ["a", "b"])
        self.assertEqual(n["views"], 1)
        self.assertEqual(n["version"], 1)

    def test_validation(self):
        bad = [dict(title=""), dict(title="  "), dict(title="x" * 101), dict(title=5),
               dict(body="x" * 10001), dict(tags=["a"] * 6), dict(tags="abc"),
               dict(tags=["A"]), dict(tags=[""]), dict(tags=["a" * 21]), dict(tags=["a_b"]),
               dict(tags=[1]), dict(tags=["a\n"])]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                self.new(**kw)
        self.assertEqual(self.counts(), [0, 0, 0])
        self.new(title="x" * 100, body="x" * 10000, tags=["a"] * 5)
        self.new(tags=("a-1", "b" * 20))

    def test_failure_leaves_nothing_and_conn_usable(self):
        def boom(step):
            raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            self.new(tags=["a"])
        self.assertEqual(self.counts(), [0, 0, 0])
        self.reset_hook()
        self.new()
        self.assertEqual(self.counts()[0], 1)

    def test_log_has_no_body(self):
        with self.assertLogs("notes", "INFO") as cm:
            nid = service.create_note(self.conn, self.alice, "t", "SECRETBODY")
        out = "\n".join(cm.output)
        self.assertNotIn("SECRETBODY", out)
        self.assertIn(str(nid), out)

    def test_audit_create(self):
        nid = self.new()
        a = service.export_audit(self.conn, self.adm)
        self.assertEqual(a[0]["action"], "create")
        self.assertEqual(a[0]["detail"], f"note={nid}")


class ViewTests(Base):
    def test_visibility(self):
        pub = self.new()
        priv = self.new(private=True)
        self.assertEqual(service.get_note(self.conn, self.bob, pub)["id"], pub)
        for u, n in [(self.bob, priv), (self.adm, priv), (self.eve, pub), (self.alice, 999)]:
            with self.assertRaises(NotFound):
                service.get_note(self.conn, u, n)
        self.assertEqual(service.get_note(self.conn, self.alice, priv)["id"], priv)

    def test_failed_view_does_not_count(self):
        priv = self.new(private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, priv)
        self.assertEqual(service.get_note(self.conn, self.alice, priv)["views"], 1)

    def test_concurrent_views(self):
        nid = self.new()
        errors = []

        def work():
            try:
                c = db.connect(self.path)
                for _ in range(10):
                    service.get_note(c, self.bob, nid)
                c.close()
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], 41)

    def test_view_race_with_hook(self):
        nid = self.new()
        other = db.connect(self.path)

        def hook(step):
            if step == "view":
                self.reset_hook()
                service.get_note(other, self.bob, nid)
        service.hooks.before_write = hook
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["views"], 2)
        other.close()


class ListSearchTests(Base):
    def test_list(self):
        for t in ["b", "a", "c"]:
            self.new(title=t)
        self.new(user=self.bob, title="pb", private=True)
        self.new(user=self.eve, title="other")
        r = service.list_notes(self.conn, self.alice, sort="title")
        self.assertEqual([i["title"] for i in r["items"]], ["a", "b", "c"])
        self.assertEqual(r["total"], 3)
        r = service.list_notes(self.conn, self.alice, page=2, per_page=2, sort="title")
        self.assertEqual([i["title"] for i in r["items"]], ["c"])
        self.assertEqual((r["page"], r["per_page"]), (2, 2))
        r = service.list_notes(self.conn, self.alice)  # 同じ updated_at なら id の大きい順
        ids = [i["id"] for i in r["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(service.list_notes(self.conn, self.bob)["total"], 4)

    def test_list_validation(self):
        for kw in [dict(page=0), dict(per_page=0), dict(per_page=51), dict(sort="id"),
                   dict(page="1"), dict(per_page=True)]:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **kw)
        service.list_notes(self.conn, self.alice, per_page=50)

    def test_search(self):
        a = self.new(title="100% sure", body="x", tags=["t1"])
        b = self.new(title="plain", body="a_b", tags=["t2"])
        c = self.new(title="abc", body="zzz")
        self.new(user=self.bob, title="100% secret", private=True)
        self.new(user=self.eve, title="100% foreign")
        s = lambda q, **kw: [r["id"] for r in service.search_notes(self.conn, self.alice, q, **kw)]
        self.assertEqual(s("%"), [a])
        self.assertEqual(s("_"), [b])
        self.assertEqual(s("a_b"), [b])
        self.assertEqual(s("100"), [a])
        self.assertEqual(s("zz"), [c])
        self.assertEqual(s("a", tags=["t1", "t2"]), [b])
        self.assertEqual(s("0", tags=["t1", "t9"]), [a])
        self.assertEqual(s("' OR 1=1 --"), [])
        self.assertEqual(s("\\"), [])

    def test_search_validation(self):
        for q, kw in [("", {}), ("  ", {}), ("x" * 101, {}), (None, {}), ("a", {"tags": ["A"]}),
                      ("a", {"tags": "ab"}), ("a", {"tags": ["a"] * 6})]:
            with self.subTest(q=q, kw=kw), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, q, **kw)


class UpdateDeleteTests(Base):
    def test_update(self):
        nid = self.new(tags=["a"])
        v = service.update_note(self.conn, self.alice, nid, 1, title="n", tags=["z"])
        self.assertEqual(v, 2)
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("n", "b", ["z"], 2))
        service.update_note(self.conn, self.adm, nid, 2, body="c")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 3, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.eadm, nid, 3, title="x")
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.alice, nid, 3, tags=["BAD"])
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.alice, nid, 3, title=" ")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["version"], 3)

    def test_private_admin(self):
        nid = self.new(private=True)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.adm, nid, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.adm, nid)

    def test_update_race_conflict(self):
        nid = self.new()
        other = db.connect(self.path)

        def hook(step):
            if step == "update":
                self.reset_hook()
                service.update_note(other, self.alice, nid, 1, title="first")
        service.hooks.before_write = hook
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, title="second")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["title"], "first")
        other.close()

    def test_update_race_delete(self):
        nid = self.new()
        other = db.connect(self.path)

        def hook(step):
            self.reset_hook()
            service.delete_note(other, self.alice, nid)
        service.hooks.before_write = hook
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, nid, 1, title="x")
        other.close()

    def test_delete(self):
        nid = self.new(tags=["a"])
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, nid)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.eve, nid)
        service.delete_note(self.conn, self.adm, nid)
        self.assertEqual(self.counts()[:2], [0, 0])
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(service.export_audit(self.conn, self.adm)[-1]["action"], "delete")

    def test_delete_failure_propagates_and_rolls_back(self):
        nid = self.new(tags=["a"])

        def boom(step):
            raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        self.reset_hook()
        self.assertEqual(self.counts()[:2], [1, 1])
        service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(self.counts()[:2], [0, 0])


class TransferTests(Base):
    def test_transfer(self):
        a = self.new()
        b = self.new(private=True)
        n = service.transfer_notes(self.conn, self.adm, self.alice["id"], self.bob["id"])
        self.assertEqual(n, 2)
        for i in (a, b):
            r = self.conn.execute("SELECT owner_id, version FROM notes WHERE id=?", (i,)).fetchone()
            self.assertEqual((r[0], r[1]), (2, 2))
        last = service.export_audit(self.conn, self.adm)[-1]
        self.assertEqual((last["action"], last["detail"]), ("transfer", "from=1 to=2 count=2"))

    def test_forbidden(self):
        self.new()
        for adm, f, t in [(self.alice, 1, 2), (self.adm, 1, 4), (self.adm, 4, 1),
                          (self.eadm, 1, 2), (self.adm, 1, 999)]:
            with self.subTest(adm=adm["id"], f=f, t=t), self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, adm, f, t)
        self.assertEqual(self.conn.execute("SELECT owner_id FROM notes").fetchone()[0], 1)

    def test_atomic(self):
        ids = [self.new() for _ in range(3)]

        def hook(step):
            if step == f"transfer:{ids[2]}":
                raise RuntimeError("boom")
        service.hooks.before_write = hook
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.adm, 1, 2)
        self.reset_hook()
        rows = self.conn.execute("SELECT owner_id, version FROM notes").fetchall()
        self.assertTrue(all((r[0], r[1]) == (1, 1) for r in rows))
        self.assertEqual(service.transfer_notes(self.conn, self.adm, 1, 2), 3)


class AuditTests(Base):
    def test_masking_and_access(self):
        self.new()
        self.new(user=self.bob)
        self.new(user=self.eve)
        a = service.export_audit(self.conn, self.adm)
        self.assertEqual(len(a), 2)
        self.assertEqual(a[0]["actor"], "alice")
        self.assertEqual(a[0]["actor_key"], "*" * 12 + "5678")
        self.assertEqual(a[1]["actor_key"], "*****")
        self.assertEqual(service._mask("12345678"), "****5678")
        self.assertEqual(service._mask("1234567"), "*******")
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)


class RateLimiterTests(unittest.TestCase):
    def test_window(self):
        t = [0.0]
        rl = RateLimiter(limit=3, window_sec=10, clock=lambda: t[0])
        self.assertEqual([rl.hit(1) for _ in range(3)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            rl.hit(1)
        self.assertEqual(rl.hit(2), 2)
        t[0] = 9.99
        with self.assertRaises(RateLimited):
            rl.hit(1)
        t[0] = 10.0
        self.assertEqual(rl.hit(1), 2)

    def test_concurrent(self):
        rl = RateLimiter(limit=3, window_sec=60)
        barrier = threading.Barrier(8)
        rl.hook = lambda: barrier.wait(5)
        ok, ng, other = [], [], []

        def work():
            try:
                rl.hit(1)
                ok.append(1)
            except RateLimited:
                ng.append(1)
            except Exception as e:  # pragma: no cover
                other.append(e)
        ts = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(other, [])
        self.assertEqual(len(ok), 3)
        self.assertEqual(len(ng), 5)


class ShareTests(unittest.TestCase):
    def test_roundtrip_and_expiry(self):
        link = make_link("s", 42, ttl_sec=100, now=1000)
        self.assertRegex(link, r"^42\.1100\.[0-9a-f]{64}$")
        self.assertEqual(verify_link("s", link, now=1099), 42)
        for now in (1100, 5000):
            with self.assertRaises(Forbidden):
                verify_link("s", link, now=now)

    def test_invalid(self):
        link = make_link("s", 42, ttl_sec=100, now=1000)
        n, e, sig = link.split(".")
        bad = [f"43.{e}.{sig}", f"{n}.9999.{sig}", link.upper(), link + "0", link[:-1],
               f"{n}.{e}", f"{n}.{e}.{sig}.x", "", "..", f"{n}.{e}.{sig[:-1]}g",
               f"٤٢.{e}.{sig}", link + "\n", None, 5]
        for b in bad:
            with self.subTest(link=b), self.assertRaises(Forbidden):
                verify_link("s", b, now=1000)
        with self.assertRaises(Forbidden):
            verify_link("other", link, now=1000)

    def test_log_no_signature(self):
        link = make_link("s", 1, now=0)
        sig = link.split(".")[2]
        with self.assertLogs("notes.share", "INFO") as cm:
            with self.assertRaises(Forbidden):
                verify_link("s", link, now=10 ** 9)
        self.assertNotIn(sig, "\n".join(cm.output))
        try:
            verify_link("s", link, now=10 ** 9)
        except Forbidden as e:
            self.assertNotIn(sig, str(e))


class SecretTests(Base):
    def test_no_secrets_in_logs_or_errors(self):
        with self.assertLogs("notes", "DEBUG") as cm:
            nid = service.create_note(self.conn, self.alice, "t", "BODYSECRET")
            service.get_note(self.conn, self.alice, nid)
            logging.getLogger("notes").info("end")
        self.assertNotIn("BODYSECRET", "\n".join(cm.output))
        self.assertNotIn("abcdefgh12345678", "\n".join(cm.output))
        out = repr(service.export_audit(self.conn, self.adm))
        self.assertNotIn("abcdefgh12345678", out)
        try:
            service.update_note(self.conn, self.bob, nid, 1, title="x")
        except Forbidden as e:
            self.assertNotIn("BODYSECRET", str(e))


if __name__ == "__main__":
    unittest.main()
