import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter
from tests.helpers import run_threads


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class RateLimitTest(unittest.TestCase):
    def test_limit回まで許し超えたらRateLimited_残りを返す(self):
        rl = RateLimiter(limit=3, window_sec=60, clock=FakeClock())
        self.assertEqual([rl.hit(1), rl.hit(1), rl.hit(1)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            rl.hit(1)
        with self.assertRaises(RateLimited):
            rl.hit(1)

    def test_利用者ごとに独立(self):
        rl = RateLimiter(limit=1, window_sec=60, clock=FakeClock())
        rl.hit(1)
        self.assertEqual(rl.hit(2), 0)
        with self.assertRaises(RateLimited):
            rl.hit(1)

    def test_枠の境界はちょうどwindow秒で新しい枠(self):
        clk = FakeClock(1000.0)
        rl = RateLimiter(limit=1, window_sec=60, clock=clk)
        rl.hit(1)
        clk.t = 1059.0  # window - 1 秒: まだ同じ枠
        with self.assertRaises(RateLimited):
            rl.hit(1)
        clk.t = 1059.999
        with self.assertRaises(RateLimited):
            rl.hit(1)
        clk.t = 1060.0  # ちょうど window 秒: 新しい枠
        self.assertEqual(rl.hit(1), 0)
        clk.t = 1119.0  # 新しい枠の開始は 1060
        with self.assertRaises(RateLimited):
            rl.hit(1)
        clk.t = 1120.0
        self.assertEqual(rl.hit(1), 0)

    def test_拒否された呼び出しは回数を増やさない(self):
        clk = FakeClock()
        rl = RateLimiter(limit=2, window_sec=10, clock=clk)
        rl.hit(1)
        rl.hit(1)
        for _ in range(5):
            with self.assertRaises(RateLimited):
                rl.hit(1)
        clk.t += 10
        self.assertEqual(rl.hit(1), 1)

    def test_既定値は10回_60秒(self):
        rl = RateLimiter(clock=FakeClock())
        for _ in range(10):
            rl.hit(1)
        with self.assertRaises(RateLimited):
            rl.hit(1)

    def test_hookは判定の後_加算の前に呼ばれ_その間の呼び出しも数える(self):
        rl = RateLimiter(limit=1, window_sec=60, clock=FakeClock())
        calls = []

        def hook():
            calls.append(1)
            if len(calls) == 1:
                rl.hit(1)  # 割り込みで枠を使い切る
        rl.hook = hook
        with self.assertRaises(RateLimited):
            rl.hit(1)
        self.assertEqual(len(calls), 2)

    def _concurrent(self, limit, threads, users=1):
        rl = RateLimiter(limit=limit, window_sec=60, clock=FakeClock())
        barrier = threading.Barrier(threads, timeout=10)
        rl.hook = lambda: barrier.wait()  # 全スレッドが判定を通過した状態を作る
        allowed = []
        denied = []
        lock = threading.Lock()

        def worker(uid):
            def run():
                try:
                    rl.hit(uid)
                    with lock:
                        allowed.append(uid)
                except RateLimited:
                    with lock:
                        denied.append(uid)
            return run

        errors = run_threads([worker(i % users) for i in range(threads)])
        self.assertEqual(errors, [])
        return allowed, denied

    def test_多スレッドの同時呼び出しで上限を超えない(self):
        allowed, denied = self._concurrent(limit=3, threads=8)
        self.assertEqual(len(allowed), 3)
        self.assertEqual(len(denied), 5)

    def test_多数スレッドでも許可数はlimitちょうど(self):
        allowed, denied = self._concurrent(limit=5, threads=40)
        self.assertEqual(len(allowed), 5)
        self.assertEqual(len(denied), 35)

    def test_利用者別でも各利用者の上限を超えない(self):
        allowed, denied = self._concurrent(limit=2, threads=24, users=3)
        for uid in range(3):
            self.assertEqual(allowed.count(uid), 2)
        self.assertEqual(len(denied), 18)
