"""V4, V5: 別接続・別スレッドでの閲覧数の加算（ファイル DB、スレッドごとに接続）。"""
import threading

from notes import service
from tests.helpers import BARRIER_TIMEOUT, FileDbCase, run_threads


class ViewsConcurrencyTest(FileDbCase):
    def test_view中に別接続が閲覧しても数え漏れない(self) -> None:
        nid = service.create_note(self.conn, self.alice, "題名", "本文です")
        other = self.new_conn()
        inner: list[int] = []
        entered = threading.local()

        def hook(step: str) -> None:
            if step == "view" and not getattr(entered, "on", False):
                entered.on = True  # 再入しない
                try:
                    inner.append(service.get_note(other, self.bob, nid)["views"])
                    inner.append(service.get_note(other, self.bob, nid)["views"])
                finally:
                    entered.on = False
        self.set_hook(hook)
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(inner, [1, 2])
        self.assertEqual(got["views"], 3)  # 読み出し済みの 0 に +1 ではなく、最新に +1
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 3)

    def test_view中に別接続が更新しても閲覧は更新後の行を返す(self) -> None:
        nid = service.create_note(self.conn, self.alice, "旧", "本文です")
        other = self.new_conn()
        done = []

        def hook(step: str) -> None:
            if step == "view" and not done:
                done.append(service.update_note(other, self.alice, nid, 1, title="新"))
        self.set_hook(hook)
        got = service.get_note(self.conn, self.bob, nid)
        self.assertEqual((got["title"], got["version"], got["views"]), ("新", 2, 1))

    def test_複数スレッドの同時閲覧でviewsの合計がNになる(self) -> None:
        n = 8
        nid = service.create_note(self.conn, self.alice, "題名", "本文です")
        barrier = threading.Barrier(n, timeout=BARRIER_TIMEOUT)

        def hook(step: str) -> None:
            if step == "view":
                barrier.wait()  # 全スレッドが「見られる」確認を終えてから書き込みに競合する
        self.set_hook(hook)

        def worker() -> int:
            c = self.new_conn_threadsafe()
            try:
                return int(service.get_note(c, self.bob, nid)["views"])
            finally:
                c.close()
        results = run_threads([worker] * n)
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, n + 1)))  # 重複なし
        self.assertEqual(self.conn.execute("SELECT views FROM notes WHERE id = ?", (nid,)).fetchone()[0], n)

    def test_hookなしの同時閲覧(self) -> None:
        n, per = 6, 5
        nid = service.create_note(self.conn, self.alice, "題名", "本文です")

        def worker() -> list[int]:
            c = self.new_conn_threadsafe()
            try:
                return [service.get_note(c, self.bob, nid)["views"] for _ in range(per)]
            finally:
                c.close()
        results = run_threads([worker] * n)
        self.assertTrue(all(isinstance(r, list) for r in results), results)
        flat = sorted(v for r in results for v in r)
        self.assertEqual(flat, list(range(1, n * per + 1)))
