"""V13: K3 閲覧・更新の同時実行（ファイル DB・別接続・別スレッド）。"""
import os
import tempfile
import threading
import unittest

from notes import db, service
from notes.errors import Conflict

TIMEOUT = 30


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        original = service.hooks.before_write
        self.addCleanup(lambda: setattr(service.hooks, "before_write", original))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "n.db")
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.alice = db.add_user(conn, 1, 1, "alice")
        self.nid = service.create_note(conn, self.alice, "t", "b")
        self.conn = conn

    def test_integration_get_複数スレッドの閲覧で数え漏れがない(self):
        n_threads, per = 6, 10
        barrier = threading.Barrier(n_threads, timeout=TIMEOUT)
        results: list[list[int]] = [[] for _ in range(n_threads)]
        errors: list[BaseException] = []

        def work(i):
            try:
                c = db.connect(self.path)
                try:
                    barrier.wait()
                    for _ in range(per):
                        results[i].append(service.get_note(c, self.alice, self.nid)["views"])
                finally:
                    c.close()
            except BaseException as e:  # noqa: BLE001  スレッド内の失敗を主スレッドで検出する
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)
            self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        total = n_threads * per
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], total)
        returned = [v for r in results for v in r]
        self.assertEqual(sorted(returned), list(range(1, total + 1)))  # 全て異なる

    def test_integration_update_逐次の割り込みで先の更新が消えない(self):
        c2 = db.connect(self.path)
        self.addCleanup(c2.close)

        def hook(step):
            if step == "update":
                self.set_plain()
                service.update_note(c2, self.alice, self.nid, 1, title="先")

        service.hooks.before_write = hook
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="後")
        row = self.conn.execute("SELECT title, version FROM notes").fetchone()
        self.assertEqual((row["title"], row["version"]), ("先", 2))

    def set_plain(self):
        service.hooks.before_write = lambda step: None

    def test_integration_update_同時の2スレッド更新は成功が1回だけ(self):
        # 両スレッドが「最初の確認の後」(hook) に同時に揃ってから書き込みに進む
        barrier = threading.Barrier(2, timeout=TIMEOUT)

        def hook(step):
            if step == "update":
                barrier.wait()

        service.hooks.before_write = hook
        outcomes: dict[str, object] = {}

        def work(name):
            c = db.connect(self.path)
            try:
                outcomes[name] = service.update_note(c, self.alice, self.nid, 1, title=name)
            except Conflict as e:
                outcomes[name] = e
            except BaseException as e:  # noqa: BLE001
                outcomes[name] = e
            finally:
                c.close()

        threads = [threading.Thread(target=work, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)
            self.assertFalse(t.is_alive())
        wins = [k for k, v in outcomes.items() if v == 2]
        conflicts = [k for k, v in outcomes.items() if isinstance(v, Conflict)]
        self.assertEqual(len(wins), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        row = self.conn.execute("SELECT title, version FROM notes").fetchone()
        self.assertEqual((row["title"], row["version"]), (wins[0], 2))
