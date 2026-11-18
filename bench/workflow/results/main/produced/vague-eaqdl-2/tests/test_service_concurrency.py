"""結合: 一時ファイル DB の 2 接続で、並行・競合・ロックを決定論的に再現する（K4 K5 K11）。"""
import sqlite3
import threading

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound
from tests.helpers import ALICE, BOB, FileCase, hook, snapshot


class ConcurrencyIntegration(FileCase):
    def test_viewフック中に別接続が閲覧しても取りこぼさない(self):  # V24
        nid = service.create_note(self.c1, self.alice, "t", "b")
        calls = []

        def other(step):
            calls.append(step)
            if step == "view" and len(calls) == 1:
                got = service.get_note(self.c2, self.alice, nid)
                self.assertEqual(got["views"], 1)
        with hook(other):
            mine = service.get_note(self.c1, self.alice, nid)
        self.assertEqual(mine["views"], 2)
        self.assertEqual(self.c1.execute("SELECT views FROM notes").fetchone()[0], 2)

    def test_view中に削除されたらNotFound(self):  # V24
        nid = service.create_note(self.c1, self.alice, "t", "b")

        def deleter(step):
            if step == "view":
                service.delete_note(self.c2, self.alice, nid)
        with hook(deleter), self.assertRaises(NotFound):
            service.get_note(self.c1, self.alice, nid)

    def test_updateフック中に別接続が先に更新するとConflictで先の内容が残る(self):  # V9 V25
        nid = service.create_note(self.c1, self.alice, "元", "元本文", ["old"])
        done = []

        def other(step):
            if step == "update" and not done:
                done.append(1)
                self.assertEqual(service.update_note(self.c2, self.alice, nid, 1, title="先",
                                                     body="先本文", tags=["first"]), 2)
        with hook(other), self.assertRaises(Conflict):
            service.update_note(self.c1, self.alice, nid, 1, title="後", body="後本文",
                                tags=["second"])
        got = service.get_note(self.c1, self.alice, nid)
        self.assertEqual((got["title"], got["body"], got["tags"], got["version"]),
                         ("先", "先本文", ["first"], 2))
        n_audit = self.c1.execute("SELECT COUNT(*) FROM audit WHERE action='update'").fetchone()[0]
        self.assertEqual(n_audit, 1)

    def test_updateフック中に別接続が削除するとNotFound(self):  # V25
        nid = service.create_note(self.c1, self.alice, "t", "b")

        def other(step):
            if step == "update":
                service.delete_note(self.c2, self.alice, nid)
        with hook(other), self.assertRaises(NotFound):
            service.update_note(self.c1, self.alice, nid, 1, title="x")

    def test_transferが途中失敗しても別接続から見て何も変わらない(self):  # V12 V27
        ids = [service.create_note(self.c1, self.alice, f"n{i}", "b") for i in range(3)]
        before = snapshot(self.c1)
        seen = []

        def boom(step):
            seen.append(step)
            if len(seen) == 1:
                # 途中経過は別接続から見えない（未コミット）
                rows = self.c2.execute("SELECT owner_id FROM notes").fetchall()
                self.assertEqual({r[0] for r in rows}, {ALICE})
            if len(seen) == 3:
                raise RuntimeError("途中失敗")
        with hook(boom), self.assertRaises(RuntimeError):
            service.transfer_notes(self.c1, self.root1, ALICE, BOB)
        self.assertEqual(seen, [f"transfer:{i}" for i in ids])
        self.assertEqual(snapshot(self.c1), before)
        self.assertEqual(snapshot(self.c2), before)
        # 失敗の後は通常どおり成功する（ロックが残っていない）
        self.assertEqual(service.transfer_notes(self.c1, self.root1, ALICE, BOB), 3)

    def _lock_holder(self):
        self.c2.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: self.c2.in_transaction and self.c2.execute("ROLLBACK"))

    def test_書き込みロック中はConflictでsqliteの例外を漏らさない(self):  # V31
        nid = service.create_note(self.c1, self.alice, "t", "b", ["a"])
        self.c1.execute("PRAGMA busy_timeout = 50")  # 待ちを短くする（既定は 5 秒）
        self._lock_holder()
        ops = {
            "create": lambda: service.create_note(self.c1, self.alice, "x", "y"),
            "get": lambda: service.get_note(self.c1, self.alice, nid),
            "update": lambda: service.update_note(self.c1, self.alice, nid, 1, title="z"),
            "delete": lambda: service.delete_note(self.c1, self.alice, nid),
            "transfer": lambda: service.transfer_notes(self.c1, self.root1, ALICE, BOB),
        }
        for name, fn in ops.items():
            with self.subTest(op=name):
                with self.assertRaises(Conflict) as cm:
                    fn()
                self.assertNotIsInstance(cm.exception, sqlite3.Error)
                self.assertNotIn("locked", str(cm.exception))  # 生のメッセージは漏らさない
                self.assertFalse(self.c1.in_transaction)
        self.c2.execute("ROLLBACK")
        # 解放後は再実行でき、ロック中の失敗は何も残していない
        self.assertEqual(service.update_note(self.c1, self.alice, nid, 1, title="z"), 2)
        self.assertEqual(self.c1.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)

    def test_読み取り系はロック中でも使える(self):  # V31
        service.create_note(self.c1, self.alice, "t", "b")
        self.c1.execute("PRAGMA busy_timeout = 50")
        self._lock_holder()
        self.assertEqual(service.list_notes(self.c1, self.alice)["total"], 1)
        self.assertEqual(len(service.search_notes(self.c1, self.alice, "t")), 1)

    def test_スレッドで同時にviewしても加算が欠けない(self):  # V24
        nid = service.create_note(self.c1, self.alice, "t", "b")
        n = 4
        errors = []
        barrier = threading.Barrier(n, timeout=10)

        def worker():
            conn = db.connect(self.path)
            try:
                barrier.wait()
                for _ in range(5):
                    service.get_note(conn, self.alice, nid)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()
        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(self.c1.execute("SELECT views FROM notes").fetchone()[0], n * 5)

    def test_更新条件のversionを使っている(self):  # V25
        """確認 SELECT をすり抜けても UPDATE の WHERE が版を見ること（Python 側の比較に頼らない）。"""
        nid = service.create_note(self.c1, self.alice, "t", "b")
        done = []

        def other(step):
            if step == "update" and not done:
                done.append(1)
                service.update_note(self.c2, self.alice, nid, 1, body="先")
        with hook(other), self.assertRaises(Conflict):
            service.update_note(self.c1, self.alice, nid, 1, title="後")
        row = self.c1.execute("SELECT title, body, version FROM notes").fetchone()
        self.assertEqual(tuple(row), ("t", "先", 2))

    def test_確認後に所有者が変わると更新はForbidden(self):  # V25
        """確認 SELECT の後に別接続が所有者を変えた場合、UPDATE の WHERE の権限条件で拒否される。"""
        nid = service.create_note(self.c1, self.alice, "t", "b")
        done = []

        def other(step):
            if step == "update" and not done:
                done.append(1)
                self.c2.execute("UPDATE notes SET owner_id = ? WHERE id = ?", (BOB, nid))
        with hook(other), self.assertRaises(Forbidden):
            service.update_note(self.c1, self.alice, nid, 1, title="x")
        row = self.c1.execute("SELECT title, owner_id, version FROM notes").fetchone()
        self.assertEqual(tuple(row), ("t", BOB, 1))

    def test_確認後に非公開へ変わると更新はNotFound(self):  # V25
        nid = service.create_note(self.c1, self.bob, "t", "b")
        done = []

        def other(step):
            if step == "update" and not done:
                done.append(1)
                self.c2.execute("UPDATE notes SET private = 1 WHERE id = ?", (nid,))
        # root1（管理者）は公開メモを更新しようとするが、直前に非公開化されて見えなくなる
        with hook(other), self.assertRaises(NotFound):
            service.update_note(self.c1, self.root1, nid, 1, title="x")
        self.assertEqual(self.c1.execute("SELECT title FROM notes").fetchone()[0], "t")
