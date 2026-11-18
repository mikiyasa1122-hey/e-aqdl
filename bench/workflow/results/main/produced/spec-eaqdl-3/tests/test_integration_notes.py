"""V2 V13 V11 V4 V14 V22 V16 V10 V21: 閲覧・更新・削除・失敗後の接続。"""
import threading

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import DbTestCase


def _boom_on(target):
    def hook(step):
        if step == target or step.startswith(target):
            raise RuntimeError("注入した失敗")
    return hook


class Visibility(DbTestCase):
    def test_可視性の表(self):
        pub = service.create_note(self.conn, self.alice, "pub", "b")
        priv = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        table = [
            (self.alice, pub, True), (self.bob, pub, True), (self.admin, pub, True),
            (self.eve, pub, False),
            (self.alice, priv, True), (self.bob, priv, False), (self.admin, priv, False),
            (self.eve, priv, False),
            (self.alice, 9999, False),
        ]
        for user, nid, ok in table:
            with self.subTest(user=user["name"], nid=nid):
                if ok:
                    self.assertEqual(service.get_note(self.conn, user, nid)["id"], nid)
                else:
                    with self.assertRaises(NotFound):
                        service.get_note(self.conn, user, nid)

    def test_閲覧数は増加後の値で返りタグは名前順(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", tags=["z", "a", "m"])
        for expected in (1, 2, 3):
            n = service.get_note(self.conn, self.bob, nid)
            self.assertEqual(n["views"], expected)
        self.assertEqual(n["tags"], ["a", "m", "z"])
        self.assertEqual(self.note_row(nid)["views"], 3)
        self.assertEqual(set(n), {"id", "org_id", "owner_id", "title", "body", "private",
                                  "version", "views", "updated_at", "tags"})

    def test_複数スレッドの同時閲覧でちょうどN回(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        n_threads, per = 8, 5
        barrier = threading.Barrier(n_threads)
        errors = []

        def work():
            try:
                c = db.connect(self.path)  # 接続はスレッドごとに作り、同じスレッドで閉じる
                try:
                    barrier.wait()
                    for _ in range(per):
                        service.get_note(c, self.bob, nid)
                finally:
                    c.close()
            except BaseException as e:  # noqa
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(n_threads)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(self.note_row(nid)["views"], n_threads * per)

    def test_viewフック中の割込み閲覧でも数え漏れない(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        other = self.new_conn()
        fired = []

        def hook(step):
            if step == "view" and not fired:
                fired.append(1)
                for _ in range(3):
                    service.get_note(other, self.alice, nid)
        self.set_hook(hook)
        r = service.get_note(self.conn, self.bob, nid)
        self.assertEqual(r["views"], 4)  # 割込み3回の後に自分の1回
        self.assertEqual(self.note_row(nid)["views"], 4)

    def test_viewフック中に削除されたらNotFound(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        other = self.new_conn()
        self.set_hook(lambda s: service.delete_note(other, self.alice, nid) if s == "view" else None)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
        self.assertFalse(self.conn.in_transaction)


class Isolation(DbTestCase):
    def test_他組織と他人の非公開に全操作が通らない(self):
        priv = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        pub = service.create_note(self.conn, self.alice, "pub", "b")
        before = self.counts()
        for outsider, nid in ((self.eve, pub), (self.eve_admin, pub),
                              (self.bob, priv), (self.admin, priv), (self.eve_admin, priv)):
            with self.subTest(op="get", u=outsider["name"], n=nid), self.assertRaises(NotFound):
                service.get_note(self.conn, outsider, nid)
            with self.subTest(op="update", u=outsider["name"], n=nid), self.assertRaises(NotFound):
                service.update_note(self.conn, outsider, nid, 1, title="x")
            with self.subTest(op="delete", u=outsider["name"], n=nid), self.assertRaises(NotFound):
                service.delete_note(self.conn, outsider, nid)
        self.assertEqual(self.counts(), before)
        self.assertEqual([self.note_row(i)["title"] for i in (priv, pub)], ["priv", "pub"])
        self.assertEqual(self.note_row(pub)["views"], 0)
        for outsider in (self.eve, self.eve_admin):
            self.assertEqual(service.list_notes(self.conn, outsider)["items"], [])
            self.assertEqual(service.search_notes(self.conn, outsider, "pub"), [])
        self.assertEqual(service.search_notes(self.conn, self.bob, "priv"), [])
        self.assertEqual(service.search_notes(self.conn, self.admin, "priv"), [])

    def test_移管は他組織を越えない(self):
        n = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.eve_admin, self.alice, self.eve)
        self.assertEqual(self.note_row(n)["owner_id"], self.alice["id"])


class UpdateDelete(DbTestCase):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "old", "body", tags=["a"])

    def test_権限表(self):
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, self.nid, 1, title="x")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, self.nid)
        self.assertEqual(self.note_row(self.nid)["title"], "old")
        self.assertEqual(service.update_note(self.conn, self.alice, self.nid, 1, title="by-owner"), 2)
        self.assertEqual(service.update_note(self.conn, self.admin, self.nid, 2, title="by-admin"), 3)
        self.assertEqual(self.note_row(self.nid)["title"], "by-admin")
        service.delete_note(self.conn, self.admin, self.nid)
        self.assertIsNone(self.note_row(self.nid))
        n2 = service.create_note(self.conn, self.alice, "t", "b")
        service.delete_note(self.conn, self.alice, n2)

    def test_非公開は管理者でもNotFound(self):
        p = service.create_note(self.conn, self.alice, "p", "b", private=True)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.admin, p, 1, title="x")
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.admin, p)
        service.update_note(self.conn, self.alice, p, 1, title="ok")

    def test_更新の内容とNone項目と監査(self):
        v = service.update_note(self.conn, self.alice, self.nid, 1, tags=["x", "x", "y"])
        row = self.note_row(self.nid)
        self.assertEqual((row["title"], row["body"], row["version"]), ("old", "body", v))
        self.assertEqual(service.get_note(self.conn, self.alice, self.nid)["tags"], ["x", "y"])
        service.update_note(self.conn, self.alice, self.nid, v, body="new")
        self.assertEqual(self.note_row(self.nid)["title"], "old")
        acts = [(r["action"], r["detail"]) for r in service.export_audit(self.conn, self.admin)]
        self.assertEqual(acts.count(("update", f"note={self.nid}")), 2)

    def test_古いversionはConflict(self):
        service.update_note(self.conn, self.alice, self.nid, 1, title="first")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="stale")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 3, title="future")
        self.assertEqual(self.note_row(self.nid)["title"], "first")

    def test_更新時も入力検証(self):
        for kw in ({"title": ""}, {"title": "x" * 101}, {"body": "x" * 10001},
                   {"tags": ["A"]}, {"tags": "abc"}, {"tags": ["a"] * 6}):
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, self.nid, 1, **kw)
        row = self.note_row(self.nid)
        self.assertEqual((row["title"], row["version"]), ("old", 1))

    def test_updateフック中の割込み更新はConflictで先の更新が残る(self):
        other = self.new_conn()
        fired = []

        def hook(step):
            if step == "update" and not fired:
                fired.append(1)
                service.update_note(other, self.admin, self.nid, 1, title="先に書いた")
        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="後から書いた", tags=["zz"])
        row = self.note_row(self.nid)
        self.assertEqual((row["title"], row["version"]), ("先に書いた", 2))
        self.assertEqual([r["tag"] for r in self.conn.execute("SELECT tag FROM note_tags")], ["a"])
        self.assertFalse(self.conn.in_transaction)

    def test_updateフック中に削除されたらNotFound(self):
        other = self.new_conn()
        self.set_hook(lambda s: service.delete_note(other, self.alice, self.nid) if s == "update" else None)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, self.nid, 1, title="x")

    def test_削除はタグとメモを消し監査を残す(self):
        service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (0, 0))
        self.assertIn(("delete", f"note={self.nid}"),
                      [(r["action"], r["detail"]) for r in service.export_audit(self.conn, self.admin)])

    def test_delete_noteフック失敗は伝わりメモもタグも残る(self):
        self.set_hook(_boom_on("delete:note"))
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (1, 1))
        self.assertEqual(self.count("audit"), 1)  # 作成の記録のみ
        self.assertFalse(self.conn.in_transaction)


class FailureRecovery(DbTestCase):
    """V10 V16 V21: 失敗後も同じ接続で続行できる。"""

    def _assert_usable(self, before):
        self.assertFalse(self.conn.in_transaction)
        self.set_hook(lambda s: None)
        nid = service.create_note(self.conn, self.alice, "after", "b")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["id"], nid)
        self.assertFalse(self.conn.in_transaction)

    def test_作成失敗後(self):
        before = self.counts()
        self.set_hook(_boom_on("create:tags"))
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "t", "b", tags=["a"])
        self.assertEqual(self.counts(), before)
        self._assert_usable(before)
        self.assertEqual(self.count("notes"), before["notes"] + 1)

    def test_閲覧失敗後(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.set_hook(_boom_on("view"))
        with self.assertRaises(RuntimeError):
            service.get_note(self.conn, self.alice, nid)
        self.assertEqual(self.note_row(nid)["views"], 0)
        self._assert_usable(None)

    def test_更新失敗後(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.set_hook(_boom_on("update"))
        with self.assertRaises(RuntimeError):
            service.update_note(self.conn, self.alice, nid, 1, title="x")
        self.assertEqual(self.note_row(nid)["version"], 1)
        self._assert_usable(None)

    def test_Conflict後(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 5, title="x")
        self._assert_usable(None)

    def test_削除失敗後(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", tags=["a"])
        self.set_hook(_boom_on("delete:note"))
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(self.note_row(nid)["version"], 1)
        self._assert_usable(None)

    def test_移管失敗後(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.set_hook(_boom_on("transfer:"))
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(self.note_row(nid)["owner_id"], self.alice["id"])
        self._assert_usable(None)

    def test_DBエラー後(self):
        # 存在しない利用者でFK違反を起こす
        ghost = {"id": 999, "org_id": 1, "name": "ghost", "role": "member", "api_key": "x"}
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            service.create_note(self.conn, ghost, "t", "b")
        self.assertEqual(self.count("notes"), 0)
        self._assert_usable(None)

    def test_入力検証失敗後(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "", "b")
        with self.assertRaises(ValidationError):
            service.list_notes(self.conn, self.alice, page=0)
        self._assert_usable(None)
