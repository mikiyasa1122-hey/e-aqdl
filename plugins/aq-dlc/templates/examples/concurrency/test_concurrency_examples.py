"""並行処理のテストの例。順序は hook・バリア・イベントで決め、固定時間の sleep に頼らない。

各テストは、期待する失敗（例外）・ロールバック・最終状態の3つを確かめる。
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory as inv  # noqa: E402

WAIT = 5  # 待機の期限（秒）。テストが固まらないように、すべての待機に付ける


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = inv.connect()
        inv.hooks.before_write = lambda step: None

    def tearDown(self):
        inv.hooks.before_write = lambda step: None  # 失敗しても差し込み口を元に戻す
        self.conn.close()

    def item(self, item_id):
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class ToctouTest(Base):
    """確認の後に、他の処理が対象を変える・消す。"""

    def test_stock_taken_between_check_and_write(self):
        iid = inv.add_item(self.conn, "pen", 1)

        def other_takes_last(step):
            if step == "reserve:check":
                inv.hooks.before_write = lambda s: None
                inv.reserve(self.conn, iid, "other")  # 確認の後に、最後の1つを先に取られる
        inv.hooks.before_write = other_takes_last
        with self.assertRaises(inv.OutOfStock):  # 期待する失敗
            inv.reserve(self.conn, iid, "mine")
        self.assertEqual(self.item(iid)["stock"], 0)  # 在庫は負にならない（不変条件: stock >= 0）
        self.assertEqual(self.count("reservations"), 1)  # 予約は他の処理の1件だけ
        self.assertFalse(self.conn.in_transaction)

    def test_item_deleted_between_check_and_write(self):
        iid = inv.add_item(self.conn, "pen", 3)

        def other_deletes(step):
            if step == "reserve:check":
                inv.delete_item(self.conn, iid)
        inv.hooks.before_write = other_deletes
        with self.assertRaises(inv.OutOfStock):
            inv.reserve(self.conn, iid, "mine")
        self.assertEqual(self.count("reservations"), 0)  # 消えた商品の予約を作らない


class LostUpdateTest(Base):
    def test_stale_version_conflicts_and_keeps_first_update(self):
        iid = inv.add_item(self.conn, "pen", 1)

        def other_renames(step):
            if step == "rename":
                inv.hooks.before_write = lambda s: None
                inv.rename(self.conn, iid, 1, "other")
        inv.hooks.before_write = other_renames
        with self.assertRaises(inv.Conflict):
            inv.rename(self.conn, iid, 1, "mine")
        row = self.item(iid)
        self.assertEqual((row["name"], row["version"]), ("other", 2))  # 先の更新を消さない


class DuplicateTest(Base):
    def test_retry_with_same_key_does_not_double_count(self):
        iid = inv.add_item(self.conn, "pen", 5)
        first = inv.reserve(self.conn, iid, "req-1")
        again = inv.reserve(self.conn, iid, "req-1")  # 再試行
        self.assertEqual(first, again)
        self.assertEqual((self.item(iid)["stock"], self.count("reservations")), (4, 1))


class RollbackTest(Base):
    def test_failure_after_stock_update_rolls_back_both(self):
        iid = inv.add_item(self.conn, "pen", 2)

        def boom(step):
            if step == "reserve:insert":
                raise RuntimeError("injected")
        inv.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            inv.reserve(self.conn, iid, "k")
        self.assertEqual((self.item(iid)["stock"], self.item(iid)["version"]), (2, 1))  # 在庫は元のまま
        self.assertEqual(self.count("reservations"), 0)
        self.assertFalse(self.conn.in_transaction)  # 接続は次の操作に使える
        inv.hooks.before_write = lambda s: None
        inv.reserve(self.conn, iid, "k")
        self.assertEqual(self.item(iid)["stock"], 1)


class ThreadsTest(unittest.TestCase):
    """別々の接続・スレッドからの同時の予約。バリアで同時に始め、待機には期限を付ける。"""

    def test_concurrent_reservations_never_oversell(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inv.db")
            setup = inv.connect(path)
            iid = inv.add_item(setup, "pen", 3)
            n = 8
            barrier = threading.Barrier(n, timeout=WAIT)
            ok, sold_out, errors = [], [], []
            lock = threading.Lock()

            def worker(i):
                conn = inv.connect(path)  # 接続はスレッドごとに1つ
                try:
                    barrier.wait()
                    inv.reserve(conn, iid, f"k{i}")
                    with lock:
                        ok.append(i)
                except inv.OutOfStock:
                    with lock:
                        sold_out.append(i)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(repr(e))
                finally:
                    conn.close()
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(WAIT * 2)
            self.assertFalse(any(t.is_alive() for t in threads), "スレッドが期限内に終わらない")
            self.assertEqual(errors, [])
            self.assertEqual((len(ok), len(sold_out)), (3, n - 3))
            row = setup.execute("SELECT stock FROM items WHERE id = ?", (iid,)).fetchone()
            self.assertEqual(row["stock"], 0)
            self.assertEqual(setup.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 3)
            setup.close()


if __name__ == "__main__":
    unittest.main()
