import threading

from notes import db, service
from notes.errors import NotFound
from tests.helpers import DbTestCase, FileDbTestCase, run_threads


class GetTest(DbTestCase):
    def test_get_閲覧のたびにviewsが増え増えた後の値を返す(self):
        nid = self.note(tags=("b", "a"))
        r1 = service.get_note(self.conn, self.u["bob"], nid)
        r2 = service.get_note(self.conn, self.u["bob"], nid)
        self.assertEqual((r1["views"], r2["views"]), (1, 2))
        self.assertEqual(r2["tags"], ["a", "b"])
        self.assertEqual(set(r2), {"id", "org_id", "owner_id", "title", "body", "private",
                                   "version", "views", "updated_at", "tags"})

    def test_get_見えないメモは全てNotFound(self):
        pub = self.note()
        priv = self.note(private=True)
        cases = [("carol", pub), ("bob", priv), ("admin", priv), ("carol", priv), ("alice", 9999)]
        for who, nid in cases:
            with self.assertRaises(NotFound, msg=f"{who} {nid}"):
                service.get_note(self.conn, self.u[who], nid)
        self.assertEqual(self.conn.execute("SELECT SUM(views) FROM notes").fetchone()[0], 0)
        # 作成者本人は非公開を見られる
        self.assertEqual(service.get_note(self.conn, self.u["alice"], priv)["views"], 1)

    def test_get_NotFoundの文言は区別がつかない(self):
        priv = self.note(private=True)
        msgs = set()
        for who, nid in [("bob", priv), ("carol", priv), ("alice", 9999)]:
            with self.assertRaises(NotFound) as cm:
                service.get_note(self.conn, self.u[who], nid)
            msgs.add(str(cm.exception))
        self.assertEqual(len(msgs), 1)

    def test_get_hook中にメモが消されたらNotFound(self):
        nid = self.note()
        self.set_hook(lambda step: self.conn.execute("DELETE FROM notes WHERE id=?", (nid,))
                      if step == "view" else None)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.u["alice"], nid)
        self.assertFalse(self.conn.in_transaction)

    def test_get_hook中に非公開にされたら他人はNotFound(self):
        nid = self.note()
        self.set_hook(lambda step: self.conn.execute("UPDATE notes SET private=1 WHERE id=?", (nid,))
                      if step == "view" else None)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.u["bob"], nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)


class GetConcurrentTest(FileDbTestCase):
    def test_get_viewフック中の並行閲覧でviewsが正確(self):
        nid = self.note()
        other = self.new_conn()
        inner = {"active": False}

        def hook(step: str) -> None:
            if step == "view" and not inner["active"]:
                inner["active"] = True
                try:
                    service.get_note(other, self.u["bob"], nid)
                finally:
                    inner["active"] = False
        self.set_hook(hook)
        r = service.get_note(self.conn, self.u["alice"], nid)
        self.assertEqual(r["views"], 2)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 2)

    def test_get_多スレッド閲覧でviews合計が一致(self):
        nid = self.note()
        n_threads, per = 6, 10
        barrier = threading.Barrier(n_threads, timeout=10)

        def worker() -> None:
            c = db.connect(self.path)  # スレッドごとに接続を作る
            try:
                barrier.wait()
                for _ in range(per):
                    service.get_note(c, self.u["bob"], nid)
            finally:
                c.close()

        errors = run_threads([worker] * n_threads)
        self.assertEqual(errors, [])
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], n_threads * per)
