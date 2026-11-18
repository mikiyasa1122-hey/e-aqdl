"""RateLimiter のテスト（V17 V18 V34）。clock と hook で時間・競合を決定論的に再現する。"""
import threading
import unittest

from notes.errors import RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class RateLimiterUnitTest(unittest.TestCase):
    def test_V17_limitまで通りlimit超過でRateLimited(self):
        clock = FakeClock(100)
        lim = RateLimiter(limit=3, window_sec=60, clock=clock)
        self.assertEqual([lim.hit("u") for _ in range(3)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            lim.hit("u")

    def test_V17_利用者ごとに独立(self):
        lim = RateLimiter(limit=1, window_sec=60, clock=FakeClock())
        self.assertEqual(lim.hit(1), 0)
        self.assertEqual(lim.hit(2), 0)
        with self.assertRaises(RateLimited):
            lim.hit(1)
        with self.assertRaises(RateLimited):
            lim.hit(2)

    def test_V17_窓が過ぎると復帰し拒否は数えない(self):
        clock = FakeClock(0)
        lim = RateLimiter(limit=2, window_sec=10, clock=clock)
        lim.hit("u")
        lim.hit("u")
        for _ in range(5):  # 拒否を繰り返しても枠は延びない・数えない
            with self.assertRaises(RateLimited):
                lim.hit("u")
        clock.now = 9.5
        with self.assertRaises(RateLimited):
            lim.hit("u")
        clock.now = 10
        self.assertEqual(lim.hit("u"), 1)  # 新しい窓の1回目
        self.assertEqual(lim.hit("u"), 0)
        with self.assertRaises(RateLimited):
            lim.hit("u")

    def test_V17_窓は最初のhitから固定(self):
        clock = FakeClock(0)
        lim = RateLimiter(limit=2, window_sec=10, clock=clock)
        lim.hit("u")
        clock.now = 9
        lim.hit("u")  # 2回目は窓を延ばさない
        clock.now = 10
        self.assertEqual(lim.hit("u"), 1)

    def test_V17_既定値(self):
        lim = RateLimiter()
        self.assertEqual((lim.limit, lim.window_sec), (10, 60))
        self.assertEqual(lim.hit("u"), 9)

    def test_V17_不正引数(self):
        for kwargs in (dict(limit=0), dict(limit=-1), dict(limit=1.5), dict(limit="3"), dict(limit=True),
                       dict(window_sec=0), dict(window_sec=-1), dict(window_sec="60"), dict(window_sec=None),
                       dict(window_sec=True)):
            with self.subTest(kwargs):
                with self.assertRaises(ValidationError):
                    RateLimiter(**kwargs)
        RateLimiter(limit=1, window_sec=0.001)


class RateLimiterConcurrencyTest(unittest.TestCase):
    def test_V18_hook内の再入でも上限を超えない(self):
        lim = RateLimiter(limit=1, window_sec=60, clock=FakeClock())
        inner = []

        entered = []

        def hook():
            if not entered:
                entered.append(1)  # 再帰を防ぐため、呼ぶ前に印を付ける
                inner.append(lim.hit("u"))  # 外側が数える前に内側が枠を使う
        lim.hook = hook
        with self.assertRaises(RateLimited):  # 外側は再判定で拒否される
            lim.hit("u")
        self.assertEqual(inner, [0])
        lim.hook = lambda: None
        with self.assertRaises(RateLimited):
            lim.hit("u")

    def test_V18_hook内の再入で残り回数が整合する(self):
        lim = RateLimiter(limit=2, window_sec=60, clock=FakeClock())
        inner = []

        entered = []

        def hook():
            if not entered:
                entered.append(1)  # 再帰を防ぐため、呼ぶ前に印を付ける
                inner.append(lim.hit("u"))
        lim.hook = hook
        self.assertEqual(lim.hit("u"), 0)  # 内側 1 回 + 外側 1 回
        self.assertEqual(inner, [1])
        with self.assertRaises(RateLimited):
            lim.hit("u")

    def test_V34_hook内で他スレッドのhitを挟んでも超えない(self):
        lim = RateLimiter(limit=2, window_sec=60, clock=FakeClock())
        results = []
        armed = [True]

        def hook():
            if armed[0]:
                armed[0] = False

                def other():
                    for _ in range(3):
                        try:
                            results.append(lim.hit("u"))
                        except RateLimited:
                            results.append("denied")
                t = threading.Thread(target=other)
                t.start()
                t.join()  # hook はロックを持たないのでデッドロックしない
        lim.hook = hook
        try:
            results.append(lim.hit("u"))
        except RateLimited:
            results.append("denied")
        passed = [r for r in results if r != "denied"]
        self.assertEqual(len(passed), 2)  # 通過は limit 回ちょうど
        self.assertEqual(results.count("denied"), 2)

    def test_V18_複数スレッドの同時hitで通過数がlimitちょうど(self):
        n_threads, limit = 16, 5
        lim = RateLimiter(limit=limit, window_sec=60, clock=FakeClock())
        barrier = threading.Barrier(n_threads)
        ok, denied = [], []

        def worker():
            barrier.wait()
            try:
                ok.append(lim.hit("u"))
            except RateLimited:
                denied.append(1)
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), limit)
        self.assertEqual(len(denied), n_threads - limit)
        self.assertEqual(sorted(ok), list(range(limit)))  # 残り回数は重複なく 0..limit-1

    def test_V18_hookで全スレッドが判定後に待ち合わせても超えない(self):
        # 全員が「枠あり」と判定した後で一斉に数える最悪の割り込みを再現する
        n_threads, limit = 6, 2
        lim = RateLimiter(limit=limit, window_sec=60, clock=FakeClock())
        barrier = threading.Barrier(n_threads)
        lim.hook = barrier.wait
        ok = []

        def worker():
            try:
                lim.hit("u")
                ok.append(1)
            except RateLimited:
                pass
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), limit)

    def test_V34_窓の境界ちょうど(self):
        clock = FakeClock(100)
        lim = RateLimiter(limit=1, window_sec=10, clock=clock)
        lim.hit("u")
        clock.now = 109.999
        with self.assertRaises(RateLimited):
            lim.hit("u")
        clock.now = 110
        self.assertEqual(lim.hit("u"), 0)

    def test_V34_拒否後に窓が過ぎれば再び通る(self):
        clock = FakeClock(0)
        lim = RateLimiter(limit=1, window_sec=5, clock=clock)
        lim.hit("u")
        with self.assertRaises(RateLimited):
            lim.hit("u")
        clock.now = 5
        self.assertEqual(lim.hit("u"), 0)


if __name__ == "__main__":
    unittest.main()
