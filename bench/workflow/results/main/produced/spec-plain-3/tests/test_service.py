import logging
import threading

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.base import Base


class CreateTest(Base):
    def test_create_and_get(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", ["b", "a", "a"])
        n = service.get_note(self.conn, self.bob, nid)
        self.assertEqual(n["tags"], ["a", "b"])
        self.assertEqual(n["views"], 1)
        self.assertEqual(n["version"], 1)
        self.assertEqual(n["owner_id"], 1)

    def test_validation(self):
        bad = [
            dict(title=""), dict(title="   "), dict(title="x" * 101), dict(title=5),
            dict(body="x" * 10001), dict(body=None),
            dict(tags="abc"), dict(tags=["A"]), dict(tags=["a_b"]), dict(tags=[""]),
            dict(tags=["x" * 21]), dict(tags=["a"] * 6), dict(tags=[1]), dict(tags=["a\n"]),
        ]
        for kw in bad:
            args = dict(title="t", body="b", tags=())
            args.update(kw)
            with self.subTest(kw=kw):
                with self.assertRaises(ValidationError):
                    service.create_note(self.conn, self.alice, **args)
        self.assertEqual(self.count("notes"), 0)
        self.assertEqual(self.count("audit"), 0)

    def test_limits_ok(self):
        service.create_note(self.conn, self.alice, "x" * 100, "y" * 10000, ["a"] * 5 + [])
        service.create_note(self.conn, self.alice, "t", "", ["x" * 20, "a-1"])

    def test_failure_rolls_back_and_conn_usable(self):
        def boom(step):
            raise RuntimeError(step)
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", ["a"])
        self.assertEqual(self.count("notes"), 0)
        self.assertEqual(self.count("note_tags"), 0)
        self.assertEqual(self.count("audit"), 0)
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = self._orig
        service.create_note(self.conn, self.alice, "t", "b")

    def test_audit_and_log(self):
        with self.assertLogs("notes", level="INFO") as cm:
            nid = service.create_note(self.conn, self.alice, "t", "SECRET-BODY")
        out = "\n".join(cm.output)
        self.assertIn(str(nid), out)
        self.assertNotIn("SECRET-BODY", out)
        log = service.export_audit(self.conn, self.admin)
        self.assertEqual(log[0]["action"], "create")
        self.assertEqual(log[0]["detail"], "note=%d" % nid)


class GetTest(Base):
    def test_visibility(self):
        pub = service.create_note(self.conn, self.alice, "t", "b")
        priv = service.create_note(self.conn, self.alice, "t", "b", private=True)
        service.get_note(self.conn, self.bob, pub)
        service.get_note(self.conn, self.alice, priv)
        for u, nid in [(self.eve, pub), (self.bob, priv), (self.admin, priv), (self.eve, priv),
                       (self.alice, 9999)]:
            with self.subTest(u=u["id"], nid=nid):
                with self.assertRaises(NotFound):
                    service.get_note(self.conn, u, nid)
        self.assertEqual(self.count("notes", "id=?", (priv,)), 1)
        self.assertEqual(service.get_note(self.conn, self.alice, priv)["views"], 2)

    def test_denied_does_not_count(self):
        pub = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.eve, pub)
        self.assertEqual(service.get_note(self.conn, self.alice, pub)["views"], 1)

    def test_concurrent_views(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        n, per = 8, 10
        errors = []

        def work():
            try:
                c = db.connect(self.path)
                try:
                    for _ in range(per):
                        service.get_note(c, self.bob, nid)
                finally:
                    c.close()
            except Exception as e:  # noqa
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], n * per + 1)

    def test_view_interleaved_with_other_view(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        other = self.new_conn()
        done = []

        def hook(step):
            if step == "view" and not done:
                done.append(1)
                service.get_note(other, self.alice, nid)
        service.hooks.before_write = hook
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], 2)

    def test_deleted_between_check_and_view(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        other = self.new_conn()
        service.hooks.before_write = lambda step: service.delete_note(other, self.alice, nid) \
            if step == "view" else None
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)
        self.assertFalse(self.conn.in_transaction)


class ListSearchTest(Base):
    def setUp(self):
        super().setUp()
        self.a = service.create_note(self.conn, self.alice, "banana", "x", ["fruit"])
        self.b = service.create_note(self.conn, self.alice, "apple", "100% sure", ["fruit", "red"])
        self.c = service.create_note(self.conn, self.alice, "cherry", "a_b", ["red"], private=True)
        self.d = service.create_note(self.conn, self.eve, "apple", "other org")

    def test_list_sorting_and_scope(self):
        r = service.list_notes(self.conn, self.bob, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [self.b, self.a])
        self.assertEqual(r["total"], 2)
        r = service.list_notes(self.conn, self.alice, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [self.b, self.a, self.c])
        r = service.list_notes(self.conn, self.alice)  # 同じ updated_at → id の大きい順
        self.assertEqual([i["id"] for i in r["items"]], [self.c, self.b, self.a])
        self.assertEqual(set(r["items"][0]), {"id", "title", "updated_at"})

    def test_paging(self):
        r = service.list_notes(self.conn, self.alice, page=2, per_page=2, sort="title")
        self.assertEqual([i["id"] for i in r["items"]], [self.c])
        self.assertEqual((r["total"], r["page"], r["per_page"]), (3, 2, 2))
        self.assertEqual(service.list_notes(self.conn, self.alice, page=9)["items"], [])

    def test_list_validation(self):
        for kw in [dict(page=0), dict(page=-1), dict(page="1"), dict(per_page=0), dict(per_page=51),
                   dict(sort="id"), dict(sort="title; drop"), dict(sort=None), dict(page=True)]:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.list_notes(self.conn, self.alice, **kw)
        service.list_notes(self.conn, self.alice, per_page=50)
        service.list_notes(self.conn, self.alice, per_page=1)

    def test_search(self):
        ids = lambda r: [x["id"] for x in r]  # noqa
        self.assertEqual(ids(service.search_notes(self.conn, self.bob, "apple")), [self.b])
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "AN")), [self.a])
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "e")), [self.b, self.c])
        self.assertEqual(service.search_notes(self.conn, self.alice, "apple")[0].keys(), {"id", "title"})

    def test_search_wildcards_literal(self):
        ids = lambda r: [x["id"] for x in r]  # noqa
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "%")), [self.b])
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "_")), [self.c])
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "a_b")), [self.c])
        self.assertEqual(service.search_notes(self.conn, self.alice, "\\"), [])
        self.assertEqual(service.search_notes(self.conn, self.alice, "' OR 1=1 --"), [])

    def test_search_tags_or(self):
        ids = lambda r: [x["id"] for x in r]  # noqa
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "e", ["fruit"])), [self.b])
        self.assertEqual(ids(service.search_notes(self.conn, self.alice, "e", ["fruit", "red"])),
                         [self.b, self.c])
        self.assertEqual(ids(service.search_notes(self.conn, self.bob, "e", ["fruit", "red"])), [self.b])

    def test_search_validation(self):
        for q in ["", "   ", "x" * 101, None, 5]:
            with self.subTest(q=q), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, q)
        for tags in ["fruit", ["BAD"], ["a"] * 6, [1]]:
            with self.subTest(tags=tags), self.assertRaises(ValidationError):
                service.search_notes(self.conn, self.alice, "a", tags)
        service.search_notes(self.conn, self.alice, "x" * 100)


class UpdateDeleteTest(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])

    def row(self):
        return self.conn.execute("SELECT * FROM notes WHERE id=?", (self.nid,)).fetchone()

    def test_update(self):
        v = service.update_note(self.conn, self.alice, self.nid, 1, title="new")
        self.assertEqual(v, 2)
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("new", "b", ["a"], 2))
        service.update_note(self.conn, self.admin, self.nid, 2, body="B", tags=["z", "y"])
        n = service.get_note(self.conn, self.alice, self.nid)
        self.assertEqual((n["body"], n["tags"], n["version"]), ("B", ["y", "z"], 3))
        service.update_note(self.conn, self.alice, self.nid, 3, tags=[])
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["tags"], [])
        actions = [a["action"] for a in service.export_audit(self.conn, self.admin)]
        self.assertEqual(actions, ["create", "update", "update", "update"])

    def test_permissions(self):
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, self.nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, self.nid)
        for fn in (lambda u: service.update_note(self.conn, u, self.nid, 1, title="x"),
                   lambda u: service.delete_note(self.conn, u, self.nid)):
            with self.assertRaises(NotFound):
                fn(self.eve)
            with self.assertRaises(NotFound):
                fn(self.eadmin)
        self.assertEqual(self.row()["title"], "t")

    def test_private_hidden_from_admin(self):
        p = service.create_note(self.conn, self.alice, "t", "b", private=True)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.admin, p, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.admin, p)
        service.update_note(self.conn, self.alice, p, 1, title="x")
        service.delete_note(self.conn, self.alice, p)

    def test_stale_version(self):
        service.update_note(self.conn, self.alice, self.nid, 1, title="x")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="y")
        self.assertEqual(self.row()["title"], "x")

    def test_conflict_when_interleaved(self):
        other = self.new_conn()
        fired = []

        def hook(step):
            if step == "update" and not fired:
                fired.append(1)
                service.update_note(other, self.admin, self.nid, 1, title="first")
        service.hooks.before_write = hook
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="second")
        self.assertEqual(self.row()["title"], "first")
        self.assertFalse(self.conn.in_transaction)

    def test_update_deleted_in_between(self):
        other = self.new_conn()
        service.hooks.before_write = lambda s: service.delete_note(other, self.alice, self.nid) \
            if s == "update" else None
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, self.nid, 1, title="x")

    def test_update_validation_leaves_note(self):
        for kw in [dict(title=""), dict(title=" "), dict(body="x" * 10001), dict(tags="abc"),
                   dict(tags=["BAD"]), dict(tags=["a"] * 6)]:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, self.nid, 1, **kw)
        n = self.row()
        self.assertEqual((n["title"], n["version"]), ("t", 1))
        self.assertEqual(self.count("note_tags"), 1)

    def test_delete(self):
        service.delete_note(self.conn, self.admin, self.nid)
        self.assertEqual(self.count("notes"), 0)
        self.assertEqual(self.count("note_tags"), 0)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual(service.export_audit(self.conn, self.admin)[-1]["detail"], "note=%d" % self.nid)

    def test_delete_failure_rolls_back(self):
        def boom(step):
            if step == "delete:note":
                raise RuntimeError("x")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.count("notes"), 1)
        self.assertEqual(self.count("note_tags"), 1)
        self.assertEqual(self.count("audit"), 1)
        service.hooks.before_write = self._orig
        service.delete_note(self.conn, self.alice, self.nid)

    def test_concurrent_updates_exactly_one_wins(self):
        n = 6
        results = []
        barrier = threading.Barrier(n)

        def work(i):
            c = db.connect(self.path)
            try:
                barrier.wait()
                service.update_note(c, self.alice, self.nid, 1, title="w%d" % i)
                results.append("ok")
            except Conflict:
                results.append("conflict")
            finally:
                c.close()
        ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["conflict"] * (n - 1) + ["ok"])
        self.assertEqual(self.row()["version"], 2)


class TransferTest(Base):
    def setUp(self):
        super().setUp()
        self.n1 = service.create_note(self.conn, self.alice, "1", "b")
        self.n2 = service.create_note(self.conn, self.alice, "2", "b", private=True)
        self.n3 = service.create_note(self.conn, self.bob, "3", "b")

    def owners(self):
        return {r["id"]: (r["owner_id"], r["version"]) for r in self.conn.execute("SELECT * FROM notes")}

    def test_transfer(self):
        n = service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(n, 2)
        self.assertEqual(self.owners(), {self.n1: (2, 2), self.n2: (2, 2), self.n3: (2, 1)})
        self.assertEqual(service.export_audit(self.conn, self.admin)[-1]["detail"],
                         "from=1 to=2 count=2")

    def test_forbidden(self):
        before = self.owners()
        for adm, f, t in [(self.alice, self.alice, self.bob), (self.eadmin, self.alice, self.bob),
                          (self.admin, self.alice, self.eve), (self.admin, self.eve, self.alice),
                          (self.eadmin, self.eve, self.admin)]:
            with self.subTest(a=adm["id"], f=f["id"], t=t["id"]), self.assertRaises(Forbidden):
                service.transfer_notes(self.conn, adm, f, t)
        self.assertEqual(self.owners(), before)

    def test_all_or_nothing(self):
        before = self.owners()

        def boom(step):
            if step == "transfer:%d" % self.n2:
                raise RuntimeError("x")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.owners(), before)
        self.assertEqual(self.count("audit", "action='transfer'"), 0)
        self.assertFalse(self.conn.in_transaction)


class AuditTest(Base):
    def test_masking(self):
        service.create_note(self.conn, self.alice, "t", "b")   # 16文字
        service.create_note(self.conn, self.bob, "t", "b")     # 5文字
        service.create_note(self.conn, self.admin, "t", "b")   # 13文字
        log = service.export_audit(self.conn, self.admin)
        self.assertEqual([r["actor"] for r in log], ["alice", "bob", "root"])
        self.assertEqual(log[0]["actor_key"], "*" * 12 + "5678")
        self.assertEqual(log[1]["actor_key"], "*****")
        self.assertEqual(log[2]["actor_key"], "*" * 9 + "9999")
        self.assertEqual(set(log[0]), {"id", "action", "detail", "actor", "actor_key"})

    def test_exactly_8_and_7(self):
        from notes.service import _mask
        self.assertEqual(_mask("12345678"), "****5678")
        self.assertEqual(_mask("1234567"), "*******")
        self.assertEqual(_mask(""), "")

    def test_admin_only_and_org_scoped(self):
        service.create_note(self.conn, self.alice, "t", "b")
        service.create_note(self.conn, self.eve, "t", "b")
        with self.assertRaises(Forbidden):
            service.export_audit(self.conn, self.alice)
        self.assertEqual(len(service.export_audit(self.conn, self.admin)), 1)
        self.assertEqual(len(service.export_audit(self.conn, self.eadmin)), 1)


class SecretLeakTest(Base):
    def test_no_body_in_errors_or_logs(self):
        with self.assertLogs("notes", level="DEBUG") as cm:
            nid = service.create_note(self.conn, self.alice, "t", "TOP-SECRET")
            logging.getLogger("notes").info("done")
        self.assertNotIn("TOP-SECRET", "\n".join(cm.output))
        try:
            service.update_note(self.conn, self.alice, nid, 99, body="NEW-SECRET")
        except Conflict as e:
            self.assertNotIn("NEW-SECRET", str(e))
        try:
            service.create_note(self.conn, self.alice, "", "BODY-SECRET")
        except ValidationError as e:
            self.assertNotIn("BODY-SECRET", str(e))
        for r in service.export_audit(self.conn, self.admin):
            self.assertNotIn("abcdefgh12345678", str(r))
            self.assertNotIn("TOP-SECRET", str(r))
