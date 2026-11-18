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
        self.alice = db.add_user(self.conn, 1, 1, "alice", "member", "alice-secret-key-1234")
        self.bob = db.add_user(self.conn, 2, 1, "bob", "member", "bob-key")
        self.admin = db.add_user(self.conn, 3, 1, "root", "admin", "admin-secret-key-9999")
        self.eve = db.add_user(self.conn, 4, 2, "eve", "member", "eve-key")
        self.eve_admin = db.add_user(self.conn, 5, 2, "eveadmin", "admin", "eveadmin-key-0000")
        self.orig_hook = service.hooks.before_write

    def tearDown(self):
        service.hooks.before_write = self.orig_hook
        self.conn.close()
        self.tmp.cleanup()

    def hook_once(self, step, fn):
        """step で一度だけ fn を呼ぶ（fn の中の呼び出しでは再び呼ばない）。"""
        state = {"done": False}

        def hook(s):
            if s == step and not state["done"]:
                state["done"] = True
                fn()
        service.hooks.before_write = hook

    def count(self, table):
        return self.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

    def new_conn(self, shared=False):
        c = db.connect(self.path)
        if shared:  # このテストのスレッドで使う接続は tearDown で閉じる
            self.addCleanup(c.close)
        return c


class CreateTests(Base):
    def test_create_and_get(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["x", "x", "a-1"])
        n = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(n["tags"], ["a-1", "x"])
        self.assertEqual(n["version"], 1)
        self.assertEqual(n["owner_id"], 1)

    def test_title_rules(self):
        for t in ["", "   ", "\n\t", "a" * 101, None, 5]:
            with self.assertRaises(ValidationError):
                service.create_note(self.conn, self.alice, t, "b")
        service.create_note(self.conn, self.alice, "a" * 100, "b")

    def test_body_rules(self):
        service.create_note(self.conn, self.alice, "t", "x" * 10000)
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "x" * 10001)

    def test_tag_rules(self):
        bad = ["A", "a_b", "", "a" * 21, "é", "a b", "a\n", 5]
        for t in bad:
            with self.assertRaises(ValidationError, msg=repr(t)):
                service.create_note(self.conn, self.alice, "t", "b", [t])
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", "abc")  # 文字列そのもの
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", ["a"] * 6)  # 重複も数える
        service.create_note(self.conn, self.alice, "t", "b", ["a" * 20, "0", "-", "a", "b"])
        self.assertEqual(self.count("notes"), 1)

    def test_rejected_leaves_nothing(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", ["ok", "BAD"])
        self.assertEqual((self.count("notes"), self.count("note_tags"), self.count("audit")), (0, 0, 0))

    def test_failure_midway_rolls_back_and_conn_usable(self):
        def boom(step):
            raise RuntimeError("x")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", ["a"])
        self.assertEqual((self.count("notes"), self.count("note_tags"), self.count("audit")), (0, 0, 0))
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = self.orig_hook
        service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual(self.count("notes"), 1)

    def test_audit_and_log(self):
        with self.assertLogs("notes", level="INFO") as cm:
            nid = service.create_note(self.conn, self.alice, "t", "SECRET-BODY")
        out = "\n".join(cm.output)
        self.assertNotIn("SECRET-BODY", out)
        self.assertIn(str(nid), out)
        a = service.export_audit(self.conn, self.admin)
        self.assertEqual((a[0]["action"], a[0]["detail"]), ("create", "note=%d" % nid))


class GetTests(Base):
    def test_visibility(self):
        pub = service.create_note(self.conn, self.alice, "t", "b")
        priv = service.create_note(self.conn, self.alice, "t", "b", private=True)
        self.assertEqual(service.get_note(self.conn, self.bob, pub)["id"], pub)
        for u, nid in [(self.bob, priv), (self.admin, priv), (self.eve, pub), (self.eve_admin, pub),
                       (self.alice, 9999)]:
            with self.assertRaises(NotFound):
                service.get_note(self.conn, u, nid)
        self.assertEqual(service.get_note(self.conn, self.alice, priv)["id"], priv)

    def test_views_increment(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual([service.get_note(self.conn, self.bob, nid)["views"] for _ in range(3)], [1, 2, 3])

    def test_not_found_does_not_count(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["views"], 1)

    def test_concurrent_views(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        n_threads, per = 6, 10
        barrier = threading.Barrier(n_threads)
        errors = []

        def work():
            c = self.new_conn()
            try:
                barrier.wait()
                for _ in range(per):
                    service.get_note(c, self.bob, nid)
            except Exception as e:  # noqa
                errors.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=work) for _ in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], n_threads * per + 1)

    def test_deleted_between_check_and_view(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        other = self.new_conn(shared=True)
        self.hook_once("view", lambda: service.delete_note(other, self.alice, nid))
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
        self.assertFalse(self.conn.in_transaction)


class ListSearchTests(Base):
    def test_list_scope_paging_sort(self):
        ids = [service.create_note(self.conn, self.alice, t, "b") for t in ["c", "a", "b"]]
        service.create_note(self.conn, self.alice, "hidden", "b", private=True)
        service.create_note(self.conn, self.eve, "other-org", "b")
        r = service.list_notes(self.conn, self.bob)
        self.assertEqual(r["total"], 3)
        self.assertEqual([i["id"] for i in r["items"]], ids[::-1])  # 同時刻でも id の大きい順
        r = service.list_notes(self.conn, self.bob, sort="title")
        self.assertEqual([i["title"] for i in r["items"]], ["a", "b", "c"])
        r = service.list_notes(self.conn, self.bob, page=2, per_page=2, sort="title")
        self.assertEqual([i["title"] for i in r["items"]], ["c"])
        self.assertEqual((r["page"], r["per_page"], r["total"]), (2, 2, 3))
        self.assertEqual(service.list_notes(self.conn, self.alice)["total"], 4)

    def test_updated_order(self):
        a = service.create_note(self.conn, self.alice, "a", "b")
        b = service.create_note(self.conn, self.alice, "b", "b")
        self.conn.execute("UPDATE notes SET updated_at = 100 WHERE id = ?", (b,))
        self.conn.execute("UPDATE notes SET updated_at = 200 WHERE id = ?", (a,))
        r = service.list_notes(self.conn, self.alice)
        self.assertEqual([i["id"] for i in r["items"]], [a, b])

    def test_list_validation(self):
        for kw in [{"page": 0}, {"page": -1}, {"per_page": 0}, {"per_page": 51}, {"sort": "id"},
                   {"page": "1"}, {"per_page": None}, {"sort": "title; DROP TABLE notes"}]:
            with self.assertRaises(ValidationError, msg=str(kw)):
                service.list_notes(self.conn, self.alice, **kw)
        service.list_notes(self.conn, self.alice, per_page=50)
        service.list_notes(self.conn, self.alice, per_page=1)

    def test_search_wildcards_literal(self):
        a = service.create_note(self.conn, self.alice, "100% sure", "b")
        b = service.create_note(self.conn, self.alice, "a_b", "x")
        service.create_note(self.conn, self.alice, "100 sure", "axb")
        c = service.create_note(self.conn, self.alice, "t", "back\\slash")
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.bob, "%")], [a])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.bob, "_")], [b])
        self.assertEqual([r["id"] for r in service.search_notes(self.conn, self.bob, "k\\s")], [c])

    def test_search_scope_and_body(self):
        a = service.create_note(self.conn, self.alice, "t", "hello world")
        service.create_note(self.conn, self.alice, "hello", "x", private=True)
        service.create_note(self.conn, self.eve, "hello", "x")
        self.assertEqual(service.search_notes(self.conn, self.bob, "hello"), [{"id": a, "title": "t"}])

    def test_search_tags_or(self):
        a = service.create_note(self.conn, self.alice, "n1", "b", ["x"])
        b = service.create_note(self.conn, self.alice, "n2", "b", ["y"])
        service.create_note(self.conn, self.alice, "n3", "b", ["z"])
        r = service.search_notes(self.conn, self.bob, "n", tags=["x", "y"])
        self.assertEqual([i["id"] for i in r], [a, b])
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.bob, "n", tags=["X"])
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.bob, "n", tags="xy")

    def test_search_validation_and_injection(self):
        for q in ["", "  ", "a" * 101, None]:
            with self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, q)
        service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual(service.search_notes(self.conn, self.alice, "' OR 1=1 --"), [])
        self.assertEqual(self.count("notes"), 1)


class UpdateDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["x"])

    def test_update_by_owner_and_admin(self):
        self.assertEqual(service.update_note(self.conn, self.alice, self.nid, 1, title="t2"), 2)
        self.assertEqual(service.update_note(self.conn, self.admin, self.nid, 2, tags=["y", "z"]), 3)
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("t2", "b", ["y", "z"], 3))

    def test_update_forbidden_and_notfound(self):
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, self.nid, 1, title="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.eve_admin, self.nid, 1, title="x")
        priv = service.create_note(self.conn, self.alice, "p", "b", private=True)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.admin, priv, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.admin, priv)
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, self.nid)

    def test_stale_version(self):
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 5, title="x")
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["title"], "t")

    def test_update_validation_keeps_data(self):
        for kw in [{"title": " "}, {"body": "x" * 10001}, {"tags": ["BAD"]}, {"tags": "abc"}]:
            with self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, self.nid, 1, **kw)
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["version"], n["tags"]), (1, ["x"]))

    def test_lost_update_is_conflict(self):
        other = self.new_conn(shared=True)
        self.hook_once("update", lambda: service.update_note(other, self.alice, self.nid, 1, title="first"))
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="second")
        service.hooks.before_write = self.orig_hook
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["title"], n["version"]), ("first", 2))
        self.assertFalse(self.conn.in_transaction)

    def test_concurrent_updates_exactly_one_wins(self):
        results = []
        barrier = threading.Barrier(4)

        def work(i):
            c = self.new_conn()
            try:
                barrier.wait()
                service.update_note(c, self.alice, self.nid, 1, title="w%d" % i)
                results.append("ok")
            except Conflict:
                results.append("conflict")
            finally:
                c.close()

        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), ["conflict"] * 3 + ["ok"])

    def test_delete(self):
        service.delete_note(self.conn, self.admin, self.nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (0, 0))
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, self.nid)
        actions = [(a["action"], a["detail"]) for a in service.export_audit(self.conn, self.admin)]
        self.assertIn(("delete", "note=%d" % self.nid), actions)

    def test_delete_failure_propagates_and_rolls_back(self):
        def boom(step):
            if step == "delete:note":
                raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, self.nid)
        service.hooks.before_write = self.orig_hook
        self.assertEqual((self.count("notes"), self.count("note_tags")), (1, 1))
        service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.count("notes"), 0)


class TransferTests(Base):
    def setUp(self):
        super().setUp()
        self.n1 = service.create_note(self.conn, self.alice, "a", "b")
        self.n2 = service.create_note(self.conn, self.alice, "p", "b", private=True)
        service.create_note(self.conn, self.bob, "bobs", "b")

    def owners(self):
        return [r[0] for r in self.conn.execute("SELECT owner_id FROM notes ORDER BY id")]

    def test_transfer_all(self):
        self.assertEqual(service.transfer_notes(self.conn, self.admin, self.alice, self.bob), 2)
        self.assertEqual(self.owners(), [2, 2, 2])
        rows = self.conn.execute("SELECT version FROM notes ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [2, 2, 1])
        last = service.export_audit(self.conn, self.admin)[-1]
        self.assertEqual((last["action"], last["detail"]), ("transfer", "from=1 to=2 count=2"))

    def test_forbidden(self):
        for admin, f, t in [(self.alice, self.alice, self.bob), (self.eve_admin, self.alice, self.bob),
                            (self.admin, self.alice, self.eve), (self.admin, self.eve, self.alice)]:
            with self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, admin, f, t)
        self.assertEqual(self.owners(), [1, 1, 2])

    def test_failure_leaves_original(self):
        def boom(step):
            if step == "transfer:%d" % self.n2:
                raise RuntimeError("boom")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.owners(), [1, 1, 2])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT version FROM notes")], [1, 1, 1])
        self.assertEqual(len(service.export_audit(self.conn, self.admin)), 3)  # 作成の3件だけ


class AuditTests(Base):
    def test_masking(self):
        service.create_note(self.conn, self.alice, "t", "b")
        service.create_note(self.conn, self.bob, "t", "b")
        service.create_note(self.conn, self.admin, "t", "b")
        service.create_note(self.conn, self.eve, "t", "b")
        a = service.export_audit(self.conn, self.admin)
        self.assertEqual(len(a), 3)  # 自分の組織だけ
        self.assertEqual([x["actor"] for x in a], ["alice", "bob", "root"])
        self.assertEqual(a[0]["actor_key"], "*" * 17 + "1234")
        self.assertEqual(a[1]["actor_key"], "*" * 7)  # 8文字未満はすべて伏せる
        self.assertEqual(a[2]["actor_key"], "*" * 17 + "9999")

    def test_eight_chars(self):
        u = db.add_user(self.conn, 9, 1, "k8", "member", "abcdefgh")
        service.create_note(self.conn, u, "t", "b")
        self.assertEqual(service.export_audit(self.conn, self.admin)[0]["actor_key"], "****efgh")

    def test_admin_only(self):
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)


class NoLeakTests(Base):
    def test_errors_and_logs_hide_secrets(self):
        with self.assertLogs("notes", level="DEBUG") as cm:
            service.create_note(self.conn, self.alice, "t", "TOP-SECRET-BODY")
            nid = service.create_note(self.conn, self.alice, "t", "TOP-SECRET-BODY", private=True)
            with self.assertRaises(NotFound) as ctx:
                service.get_note(self.conn, self.bob, nid)
        self.assertNotIn("TOP-SECRET-BODY", "\n".join(cm.output) + str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            service.create_note(self.conn, self.alice, "", "TOP-SECRET-BODY")
        self.assertNotIn("TOP-SECRET-BODY", str(ctx.exception))
        out = str(service.export_audit(self.conn, self.admin))
        self.assertNotIn("alice-secret-key-1234", out)


if __name__ == "__main__":
    unittest.main()
