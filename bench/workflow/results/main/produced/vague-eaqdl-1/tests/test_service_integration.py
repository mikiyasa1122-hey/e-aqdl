"""service の結合テスト（ファイル DB の別接続と hooks で競合・失敗を決定論的に再現する）。"""
import logging
import sqlite3
import threading
import unittest

from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests import helpers
from tests.helpers import KEYS, check_org_isolation, check_update_conflict, make_env, make_file_env, \
    orphan_tags, set_hook, snapshot


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class IsolationTest(unittest.TestCase):
    def test_V1_組織Bのメモは組織Aから見えず触れない(self):
        check_org_isolation(self)

    def test_V2_組織Aの管理者でも組織Bに触れず監査にも出ない(self):
        conn, u = make_env()
        b_id = service.create_note(conn, u[12], "b-note", "b", private=False)
        b_priv = service.create_note(conn, u[12], "b-priv", "b", private=True)
        a_id = service.create_note(conn, u[2], "a-note", "a")
        before = snapshot(conn)
        for nid in (b_id, b_priv):
            with self.assertRaises(NotFound):
                service.update_note(conn, u[1], nid, 1, title="hack")
            with self.assertRaises(NotFound):
                service.delete_note(conn, u[1], nid)
        # 組織Bの利用者への transfer / 組織Bの利用者からの transfer
        with self.assertRaises(NotFound):
            service.transfer_notes(conn, u[1], 2, 12)
        with self.assertRaises(NotFound):
            service.transfer_notes(conn, u[1], 12, 2)
        self.assertEqual(snapshot(conn), before)
        rows = service.export_audit(conn, u[1])
        self.assertEqual([r["detail"] for r in rows], ["note=%d" % a_id])
        rows_b = service.export_audit(conn, u[11])
        self.assertEqual([r["detail"] for r in rows_b], ["note=%d" % b_id, "note=%d" % b_priv])

    def test_V26_別組織への引き継ぎと監査の混入がない(self):
        conn, u = make_env()
        a = service.create_note(conn, u[2], "a", "x")
        b = service.create_note(conn, u[12], "b", "x")
        with self.assertRaises(NotFound):
            service.transfer_notes(conn, u[1], 2, 12)
        with self.assertRaises(NotFound):
            service.transfer_notes(conn, u[11], 12, 2)
        # 同組織の引き継ぎは他組織のメモに影響しない
        self.assertEqual(service.transfer_notes(conn, u[1], 2, 3), 1)
        owners = {r["id"]: r["owner_id"] for r in conn.execute("SELECT id, owner_id FROM notes")}
        self.assertEqual(owners, {a: 3, b: 12})
        org1 = [r["action"] for r in service.export_audit(conn, u[1])]
        org2 = [r["action"] for r in service.export_audit(conn, u[11])]
        self.assertEqual(org1, ["create", "transfer"])
        self.assertEqual(org2, ["create"])
        self.assertEqual(conn.execute("SELECT version FROM notes WHERE id = ?", (b,)).fetchone()[0], 1)


class PrivateTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()
        self.pid = service.create_note(self.conn, self.u[2], "my-private", "secret-body", tags=["t"], private=True)

    def test_V3_privateは他memberと管理者に見えない(self):
        for user in (self.u[3], self.u[1]):
            with self.assertRaises(NotFound):
                service.get_note(self.conn, user, self.pid)
            self.assertEqual(service.list_notes(self.conn, user)["total"], 0)
            self.assertEqual(service.search_notes(self.conn, user, "my-private"), [])
            self.assertEqual(service.search_notes(self.conn, user, "secret-body", tags=["t"]), [])
        # 所有者には見える
        self.assertEqual(service.list_notes(self.conn, self.u[2])["total"], 1)
        self.assertEqual(service.get_note(self.conn, self.u[2], self.pid)["title"], "my-private")
        # 見えない get は閲覧数を増やさない
        self.assertEqual(self.conn.execute("SELECT views FROM notes WHERE id = ?", (self.pid,)).fetchone()[0], 1)

    def test_V4_他人のprivateは管理者でもNotFound_所有者は成功(self):
        before = snapshot(self.conn)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u[1], self.pid, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.u[1], self.pid)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.u[3], self.pid, 1, title="x")
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(service.update_note(self.conn, self.u[2], self.pid, 1, title="mine"), 2)
        service.delete_note(self.conn, self.u[2], self.pid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)

    def test_V27_privateの有無が他者から見えない(self):
        conn, u = make_env()
        service.create_note(conn, u[2], "pub", "x", tags=["t"])
        total_before = service.list_notes(conn, u[3])["total"]
        found_before = [i["id"] for i in service.search_notes(conn, u[3], "p", tags=["t"])]
        audit_before = [(r["action"], r["detail"]) for r in service.export_audit(conn, u[1])]
        pid = service.create_note(conn, u[2], "pup", "x", tags=["t"], private=True)
        self.assertEqual(service.list_notes(conn, u[3])["total"], total_before)
        self.assertEqual([i["id"] for i in service.search_notes(conn, u[3], "p", tags=["t"])], found_before)
        self.assertEqual([i["id"] for i in service.search_notes(conn, u[1], "p", tags=["t"])], found_before)
        # 監査の detail は id だけ（題名・タグを含まない）
        audit_after = service.export_audit(conn, u[1])
        self.assertEqual([(r["action"], r["detail"]) for r in audit_after][:len(audit_before)], audit_before)
        self.assertEqual(audit_after[-1]["detail"], "note=%d" % pid)
        for r in audit_after:
            self.assertNotIn("pup", r["detail"])
        # 管理者は private を更新・削除できない
        with self.assertRaises(NotFound):
            service.update_note(conn, u[1], pid, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(conn, u[1], pid)


class ViewsTest(unittest.TestCase):
    def test_V7_連続getでviewsが増えversionは不変(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b")
        base = service.get_note(conn, u[2], nid)
        self.assertEqual(base["views"], 1)
        for expected in (2, 3):
            got = service.get_note(conn, u[3], nid)
            self.assertEqual(got["views"], expected)
            self.assertEqual((got["version"], got["updated_at"]), (base["version"], base["updated_at"]))

    def test_V7_V29_hook内で別接続がgetしても加算が失われない(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b")
        calls = []

        def hook(step):
            if step == "view" and not calls:
                calls.append(1)
                got = service.get_note(c2, u[3], nid)
                calls.append(got["views"])
        set_hook(self, hook)
        got = service.get_note(c1, u[2], nid)
        self.assertEqual(calls, [1, 1])  # 別接続が先に確定させ 1
        self.assertEqual(got["views"], 2)  # 自分の加算は失われない
        row = c1.execute("SELECT views, version FROM notes WHERE id = ?", (nid,)).fetchone()
        self.assertEqual(tuple(row), (2, 1))

    def test_V29_複数スレッドの同時getで加算が失われない(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b")
        from notes import db
        barrier = threading.Barrier(4)
        errors = []

        def worker():
            conn = db.connect(path)
            try:
                barrier.wait()
                for _ in range(5):
                    service.get_note(conn, u[2], nid)
            except Exception as exc:  # noqa
                errors.append(exc)
            finally:
                conn.close()
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(c1.execute("SELECT views FROM notes WHERE id = ?", (nid,)).fetchone()[0], 20)

    def test_V29_getの後もversionは不変で同じversionで更新できる(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b")
        service.get_note(conn, u[2], nid)
        service.get_note(conn, u[3], nid)
        self.assertEqual(service.get_note(conn, u[2], nid)["version"], 1)
        self.assertEqual(service.update_note(conn, u[2], nid, 1, title="x"), 2)

    def test_V7_getの途中で削除されるとNotFound(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b")

        def hook(step):
            if step == "view":
                service.delete_note(c2, u[2], nid)
        set_hook(self, hook)
        with self.assertRaises(NotFound):
            service.get_note(c1, u[2], nid)
        self.assertFalse(c1.in_transaction)


class AtomicityTest(unittest.TestCase):
    def test_V28_検証失敗とhook失敗で行が残らない(self):
        conn, u = make_env()
        before = snapshot(conn)
        with self.assertRaises(ValidationError):
            service.create_note(conn, u[2], "", "b", tags=["a"])
        self.assertEqual(snapshot(conn), before)

        def hook(step):
            if step == "create:tags":
                raise RuntimeError("boom")
        set_hook(self, hook)
        with self.assertRaises(RuntimeError):
            service.create_note(conn, u[2], "t", "b", tags=["a"])
        self.assertEqual(snapshot(conn), before)
        self.assertFalse(conn.in_transaction)

    def test_V21_全書き込みの途中失敗で元に戻り接続が使える(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b", tags=["a"])
        other = service.create_note(conn, u[3], "o", "b")
        steps = {"create": "create:tags", "update": "update", "delete": "delete:note",
                 "transfer": "transfer:%d" % nid}
        ops = {
            "create": lambda: service.create_note(conn, u[2], "n", "b", tags=["z"]),
            "update": lambda: service.update_note(conn, u[2], nid, 1, title="x", tags=["q"]),
            "delete": lambda: service.delete_note(conn, u[2], nid),
            "transfer": lambda: service.transfer_notes(conn, u[1], 2, 3),
        }
        for name, step in steps.items():
            with self.subTest(name):
                before = snapshot(conn)

                def hook(s, step=step):
                    if s == step:
                        raise RuntimeError("fail-" + step)
                set_hook(self, hook)
                with self.assertRaisesRegex(RuntimeError, "fail-"):
                    ops[name]()
                self.assertEqual(snapshot(conn), before)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(orphan_tags(conn), 0)
                service.hooks.before_write = lambda s: None
                self.assertEqual(service.get_note(conn, u[3], other)["id"], other)  # 直後の別操作が成功

    def test_V12_deleteでタグごと消える_途中失敗で残る(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b", tags=["a", "b"])
        before = snapshot(conn)

        def hook(step):
            if step == "delete:note":
                # タグは消した後の時点
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
                raise RuntimeError("boom")
        set_hook(self, hook)
        with self.assertRaises(RuntimeError):
            service.delete_note(conn, u[2], nid)
        self.assertEqual(snapshot(conn), before)
        service.hooks.before_write = lambda s: None
        service.delete_note(conn, u[2], nid)
        self.assertEqual(snapshot(conn)["note_tags"], [])
        self.assertEqual(len(snapshot(conn)["notes"]), 0)
        with self.assertRaises(NotFound):
            service.delete_note(conn, u[2], nid)

    def test_V36_書き込みロック中は部分更新を残さず例外で失敗(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b", tags=["a"])
        nid3 = service.create_note(c1, u[3], "t3", "b")
        c1.execute("PRAGMA busy_timeout = 0")
        before = snapshot(c1)
        c2.execute("BEGIN IMMEDIATE")
        ops = [lambda: service.create_note(c1, u[2], "n", "b", tags=["x"]),
               lambda: service.get_note(c1, u[2], nid),
               lambda: service.update_note(c1, u[2], nid, 1, title="x", tags=["q"]),
               lambda: service.delete_note(c1, u[2], nid),
               lambda: service.transfer_notes(c1, u[1], 2, 3)]
        for op in ops:
            with self.assertRaises(sqlite3.OperationalError):
                op()
            self.assertFalse(c1.in_transaction)
        c2.execute("ROLLBACK")
        self.assertEqual(snapshot(c1), before)
        # ロックが外れたら次の操作に使える
        self.assertEqual(service.update_note(c1, u[2], nid, 1, title="ok"), 2)

    def test_V36_ロック待ち後のコミット失敗でもトランザクションが残らない(self):
        # 書き込み中に例外（hook）が出た後でも ROLLBACK 済みで他接続が書ける
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b")

        def hook(step):
            raise RuntimeError("x")
        set_hook(self, hook)
        with self.assertRaises(RuntimeError):
            service.delete_note(c1, u[2], nid)
        c2.execute("BEGIN IMMEDIATE")  # c1 がロックを持ち越していれば失敗する
        c2.execute("ROLLBACK")


class ConcurrencyTest(unittest.TestCase):
    def test_V11_V30_同じversionの2者の更新は片方だけ成功(self):
        check_update_conflict(self)

    def test_V30_先の更新内容が残り後の更新は反映されない(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "orig", "body", tags=["a"])
        done = []

        def hook(step):
            if step == "update" and not done:
                done.append(1)
                service.update_note(c2, u[1], nid, 1, body="B1", tags=["first"])
        set_hook(self, hook)
        with self.assertRaises(Conflict):
            service.update_note(c1, u[2], nid, 1, title="T2", tags=["second"])
        got = service.get_note(c1, u[2], nid)
        self.assertEqual((got["title"], got["body"], got["version"], got["tags"]), ("orig", "B1", 2, ["first"]))
        self.assertEqual([r["action"] for r in service.export_audit(c1, u[1])], ["create", "update"])

    def test_V11_古いversionはConflict(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b")
        service.update_note(conn, u[2], nid, 1, title="x")
        with self.assertRaises(Conflict):
            service.update_note(conn, u[2], nid, 1, title="y")

    def test_V11_V31_更新と削除の競合で復活せず孤児も残らない(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b", tags=["a", "b"])
        done = []

        def hook(step):
            if step == "update" and not done:
                done.append(1)
                service.delete_note(c2, u[2], nid)
        set_hook(self, hook)
        with self.assertRaises((NotFound, Conflict)):
            service.update_note(c1, u[2], nid, 1, title="x", tags=["new"])
        self.assertEqual(c1.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        self.assertEqual(c1.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        self.assertEqual(orphan_tags(c1), 0)
        self.assertFalse(c1.in_transaction)
        self.assertEqual([r["action"] for r in service.export_audit(c1, u[1])], ["create", "delete"])

    def test_V31_更新確定後の削除はタグの孤児を残さない(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b", tags=["a"])
        service.update_note(c2, u[2], nid, 1, title="other-change", tags=["b", "c"])
        service.delete_note(c1, u[2], nid)
        self.assertEqual(orphan_tags(c1), 0)
        self.assertEqual(c1.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        self.assertEqual(c1.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)

    def test_V31_所有者が引き継がれた後の更新は再確認で拒否される(self):
        path, c1, c2, u = make_file_env(self)
        nid = service.create_note(c1, u[2], "t", "b")
        done = []

        def hook(step):
            if step == "update" and not done:
                done.append(1)
                service.transfer_notes(c2, u[1], 2, 3)  # 所有者が 2 -> 3 に変わる
        set_hook(self, hook)
        with self.assertRaises(Forbidden):
            service.update_note(c1, u[2], nid, 1, title="x")
        row = c1.execute("SELECT title, owner_id, version FROM notes WHERE id = ?", (nid,)).fetchone()
        self.assertEqual(tuple(row), ("t", 3, 2))

    def test_V31_他memberの更新削除は拒否(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b", tags=["a"])
        before = snapshot(conn)
        with self.assertRaises(Forbidden):
            service.update_note(conn, u[3], nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(conn, u[3], nid)
        self.assertEqual(snapshot(conn), before)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()
        c, u = self.conn, self.u
        self.n1 = service.create_note(c, u[2], "n1", "b")
        self.n2 = service.create_note(c, u[2], "n2", "b", private=True)
        self.n3 = service.create_note(c, u[3], "n3", "b")
        self.other_org = service.create_note(c, u[12], "o", "b")

    def _owners(self):
        return {r["id"]: (r["owner_id"], r["version"]) for r in
                self.conn.execute("SELECT id, owner_id, version FROM notes")}

    def test_V13_全メモ移動_private含む_件数と監査(self):
        n = service.transfer_notes(self.conn, self.u[1], 2, 3)
        self.assertEqual(n, 2)
        self.assertEqual(self._owners(), {self.n1: (3, 2), self.n2: (3, 2), self.n3: (3, 1), self.other_org: (12, 1)})
        last = service.export_audit(self.conn, self.u[1])[-1]
        self.assertEqual((last["action"], last["detail"]), ("transfer", "from=2 to=3 count=2"))
        self.assertEqual(sum(1 for r in service.export_audit(self.conn, self.u[1]) if r["action"] == "transfer"), 1)
        # 移した private は新しい所有者だけに見える
        self.assertEqual(service.get_note(self.conn, self.u[3], self.n2)["title"], "n2")
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.u[2], self.n2)

    def test_V13_0件でも成功しcount0を記録(self):
        self.assertEqual(service.transfer_notes(self.conn, self.u[1], 1, 3), 0)
        self.assertEqual(service.export_audit(self.conn, self.u[1])[-1]["detail"], "from=1 to=3 count=0")

    def test_V13_dictでも渡せる(self):
        self.assertEqual(service.transfer_notes(self.conn, self.u[1], self.u[2], self.u[3]), 2)

    def test_V14_拒否されるケースは何も変えない(self):
        before = snapshot(self.conn)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.u[2], 2, 3)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u[1], 2, 999)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u[1], 999, 3)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u[1], 2, 12)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u[1], 12, 3)
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.u[1], 2, 2)
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.u[1], "2", 3)
        self.assertEqual(snapshot(self.conn), before)
        self.assertFalse(self.conn.in_transaction)

    def test_V14_V32_2件目の失敗で1件目も戻る(self):
        before = snapshot(self.conn)
        seen = []

        def hook(step):
            seen.append(step)
            if step == "transfer:%d" % self.n2:
                raise RuntimeError("boom")
        set_hook(self, hook)
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.u[1], 2, 3)
        self.assertEqual(seen, ["transfer:%d" % self.n1, "transfer:%d" % self.n2])
        self.assertEqual(snapshot(self.conn), before)
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = lambda s: None
        self.assertEqual(service.transfer_notes(self.conn, self.u[1], 2, 3), 2)  # 再試行できる

    def test_V32_存在しないtoで何も変わらずhookも呼ばれない(self):
        before = snapshot(self.conn)
        seen = []
        set_hook(self, lambda step: seen.append(step))
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.u[1], 2, 999)
        self.assertEqual(seen, [])
        self.assertEqual(snapshot(self.conn), before)


class AuditTest(unittest.TestCase):
    def test_V15_memberはForbidden_管理者は自組織のみid順(self):
        conn, u = make_env()
        a = service.create_note(conn, u[2], "t", "b", tags=["x"])
        service.get_note(conn, u[2], a)
        service.list_notes(conn, u[2])
        service.search_notes(conn, u[2], "t")
        service.update_note(conn, u[2], a, 1, title="u")
        service.create_note(conn, u[12], "other", "b")
        service.transfer_notes(conn, u[1], 2, 3)
        service.delete_note(conn, u[3], a)
        with self.assertRaises(Forbidden):
            service.export_audit(conn, u[2])
        with self.assertRaises(Forbidden):
            service.export_audit(conn, u[12])
        rows = service.export_audit(conn, u[1])
        self.assertEqual([(r["action"], r["detail"]) for r in rows],
                         [("create", "note=%d" % a), ("update", "note=%d" % a),
                          ("transfer", "from=2 to=3 count=1"), ("delete", "note=%d" % a)])
        self.assertEqual([r["actor"] for r in rows], ["a2", "a2", "a1", "a3"])
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(service.export_audit(conn, u[11])), 1)

    def test_V15_失敗した操作は監査に残らない(self):
        conn, u = make_env()
        nid = service.create_note(conn, u[2], "t", "b")
        for func in (lambda: service.update_note(conn, u[3], nid, 1, title="x"),
                     lambda: service.update_note(conn, u[2], nid, 9, title="x"),
                     lambda: service.delete_note(conn, u[12], nid)):
            with self.assertRaises(Exception):
                func()
        self.assertEqual(len(service.export_audit(conn, u[1])), 1)

    def test_V33_api_keyが出力詳細ログのどこにも出ない(self):
        conn, u = make_env()
        handler = _Collect()
        names = ("notes", "notes.share")
        for name in names:
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
            self.addCleanup(lg.removeHandler, handler)
        nid = service.create_note(conn, u[2], "t", "b", tags=["x"])
        service.get_note(conn, u[2], nid)
        service.update_note(conn, u[2], nid, 1, title="u")
        service.transfer_notes(conn, u[1], 2, 3)
        service.delete_note(conn, u[3], nid)
        rows = service.export_audit(conn, u[1])
        blob = repr(rows) + "".join(r["detail"] for r in rows) + "\n".join(handler.messages)
        all_details = "".join(r[0] for r in conn.execute("SELECT detail FROM audit"))
        for key in KEYS.values():
            self.assertNotIn(key, blob)
            self.assertNotIn(key, all_details)
            self.assertNotIn(key[:-4], blob)
        self.assertTrue(handler.messages)  # ログは出ている
        with self.assertRaises(Forbidden):
            service.export_audit(conn, u[3])


if __name__ == "__main__":
    unittest.main()
