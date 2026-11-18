"""V11: update フックで別接続が更新した後の Conflict と、先の更新の保持。"""
import threading

from notes import service
from notes.errors import Conflict
from tests.helpers import BARRIER_TIMEOUT, FileDbCase, run_threads


class UpdateConflictTest(FileDbCase):
    def test_updateフックで別接続が更新するとConflictで先の更新が残る(self) -> None:
        nid = service.create_note(self.conn, self.alice, "元", "本文です", tags=["old"])
        other = self.new_conn()
        done: list[int] = []
        entered: list[bool] = []

        def hook(step: str) -> None:
            if step == "update" and not entered:
                entered.append(True)  # 先に印を付け、割り込み側の update で再入しない
                done.append(service.update_note(other, self.alice, nid, 1, title="先の更新", tags=["first"]))
        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, title="後の更新", tags=["second"])
        self.assertEqual(done, [2])
        row = self.conn.execute("SELECT title, version FROM notes WHERE id = ?", (nid,)).fetchone()
        self.assertEqual((row["title"], row["version"]), ("先の更新", 2))
        tags = [r[0] for r in self.conn.execute("SELECT tag FROM note_tags WHERE note_id = ?", (nid,))]
        self.assertEqual(tags, ["first"])
        self.assertFalse(self.conn.in_transaction)
        # 最新の version なら成功する
        self.assertEqual(service.update_note(self.conn, self.alice, nid, 2, title="再試行"), 3)

    def test_フック中に別接続が削除してもNotFound(self) -> None:
        from notes.errors import NotFound
        nid = service.create_note(self.conn, self.alice, "元", "本文です")
        other = self.new_conn()
        done: list[bool] = []

        def hook(step: str) -> None:
            if step == "update" and not done:
                done.append(True)
                service.delete_note(other, self.alice, nid)
        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, nid, 1, title="x")

    def test_同じversionでの同時更新は片方だけ成功する(self) -> None:
        n = 4
        nid = service.create_note(self.conn, self.alice, "元", "本文です")
        barrier = threading.Barrier(n, timeout=BARRIER_TIMEOUT)

        def hook(step: str) -> None:
            if step == "update":
                barrier.wait()  # 全員が version=1 を確認してから書き込みに入る
        self.set_hook(hook)

        def worker(i: int):
            def run() -> int:
                c = self.new_conn_threadsafe()
                try:
                    return service.update_note(c, self.alice, nid, 1, title=f"更新{i}")
                finally:
                    c.close()
            return run
        results = run_threads([worker(i) for i in range(n)])
        ok = [r for r in results if r == 2]
        conflicts = [r for r in results if isinstance(r, Conflict)]
        self.assertEqual((len(ok), len(conflicts)), (1, n - 1), results)
        self.assertEqual(self.conn.execute("SELECT version FROM notes").fetchone()[0], 2)
