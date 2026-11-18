"""V7 V15: RateLimiter。"""
import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Limiter(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.rl = RateLimiter(limit=3, window_sec=60, clock=self.clock)

    def test_limit回まで許可し残りを返す(self):
        self.assertEqual([self.rl.hit(1) for _ in range(3)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        with self.assertRaises(RateLimited):
            self.rl.hit(1)

    def test_枠の境界(self):
        for _ in range(3):
            self.rl.hit(1)
        self.clock.t += 59.999
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.clock.t = 1000.0 + 60
        self.assertEqual(self.rl.hit(1), 2)  # ちょうど window 秒で新しい枠
        # 新しい枠の開始は 1060。そこから 59.999 秒は同じ枠
        self.rl.hit(1)
        self.rl.hit(1)
        self.clock.t = 1060.0 + 59.999
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.clock.t = 1060.0 + 60
        self.assertEqual(self.rl.hit(1), 2)

    def test_利用者ごとに独立(self):
        for _ in range(3):
            self.rl.hit(1)
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.assertEqual(self.rl.hit(2), 2)

    def test_拒否は回数に数えない(self):
        for _ in range(3):
            self.rl.hit(1)
        for _ in range(5):
            with self.assertRaises(RateLimited):
                self.rl.hit(1)
        self.clock.t += 60
        self.assertEqual(self.rl.hit(1), 2)

    def test_hook内の同時hitでも上限を超えない(self):
        self.rl.hit(1)
        self.rl.hit(1)  # 残り1
        results, fired = [], []

        def run():
            try:
                results.append(self.rl.hit(1))
            except RateLimited:
                results.append("limited")

        def hook():
            if not fired:
                fired.append(1)
                t = threading.Thread(target=run)  # 判定後・加算前に別スレッドが最後の1回を使う
                t.start()
                t.join()
        self.rl.hook = hook
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.assertEqual(results, [0])
        self.rl.hook = lambda: None
        with self.assertRaises(RateLimited):
            self.rl.hit(1)

    def test_実スレッドの同時hitで許可はlimit回だけ(self):
        rl = RateLimiter(limit=5, window_sec=60, clock=self.clock)
        barrier = threading.Barrier(20)
        ok, ng = [], []

        def run():
            barrier.wait()
            try:
                ok.append(rl.hit(1))
            except RateLimited:
                ng.append(1)
        ts = [threading.Thread(target=run) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(ok), len(ng)), (5, 15))
        self.assertEqual(sorted(ok), [0, 1, 2, 3, 4])
