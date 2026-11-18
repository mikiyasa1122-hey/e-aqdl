"""AC14 AC15: RateLimiter。"""
import threading
import unittest

from notes.errors import RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RateLimiterUnit(unittest.TestCase):
    def test_limit回目まで成功し残り回数を返す_超えたらRateLimited(self):  # V14 V29
        clock = FakeClock()
        rl = RateLimiter(limit=3, window_sec=60, clock=clock)
        self.assertEqual([rl.hit(1) for _ in range(3)], [2, 1, 0])
        for _ in range(2):  # 失敗は回数に数えない
            with self.assertRaises(RateLimited):
                rl.hit(1)

    def test_窓の境界(self):  # V14 V29
        clock = FakeClock()
        rl = RateLimiter(limit=2, window_sec=60, clock=clock)
        rl.hit(1)
        rl.hit(1)
        clock.t += 59.999
        with self.assertRaises(RateLimited):
            rl.hit(1)
        clock.t += 0.001  # ちょうど window_sec 経過
        self.assertEqual(rl.hit(1), 1)
        self.assertEqual(rl.hit(1), 0)
        with self.assertRaises(RateLimited):
            rl.hit(1)

    def test_利用者ごとに独立(self):  # V14
        rl = RateLimiter(limit=1, window_sec=60, clock=FakeClock())
        rl.hit("a")
        with self.assertRaises(RateLimited):
            rl.hit("a")
        self.assertEqual(rl.hit("b"), 0)

    def test_既定値(self):  # V14
        rl = RateLimiter(clock=FakeClock())
        self.assertEqual((rl.limit, rl.window_sec), (10, 60))
        self.assertEqual(rl.hit(1), 9)

    def test_不正なlimitとwindow_sec(self):  # V14
        for kw in ({"limit": 0}, {"limit": -1}, {"limit": 1.5}, {"limit": True},
                   {"limit": "3"}, {"window_sec": 0}, {"window_sec": -1},
                   {"window_sec": float("nan")}, {"window_sec": float("inf")},
                   {"window_sec": True}, {"window_sec": "60"}, {"window_sec": None}):
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                RateLimiter(**kw)
        RateLimiter(limit=1, window_sec=0.5)


class RateLimiterIntegration(unittest.TestCase):
    def _run_concurrently(self, rl, n, user=1):
        barrier = threading.Barrier(n, timeout=10)
        rl.hook = lambda: barrier.wait()
        results = []
        lock = threading.Lock()

        def worker():
            try:
                r = rl.hit(user)
            except RateLimited:
                r = "limited"
            except Exception as exc:  # noqa: BLE001  ハングや想定外を結果に残す
                r = exc
            with lock:
                results.append(r)
        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertFalse(any(t.is_alive() for t in threads))
        return results

    def test_同時に判定へ通しても成功数はlimit以下(self):  # V15 V29
        rl = RateLimiter(limit=3, window_sec=60, clock=FakeClock())
        results = self._run_concurrently(rl, 8)
        ok = [r for r in results if isinstance(r, int)]
        self.assertEqual(len(results), 8)
        self.assertEqual(len(ok), 3)
        self.assertEqual(sorted(ok), [0, 1, 2])
        self.assertEqual(results.count("limited"), 5)

    def test_上限未満なら全員成功(self):  # V15
        rl = RateLimiter(limit=10, window_sec=60, clock=FakeClock())
        results = self._run_concurrently(rl, 4)
        self.assertEqual(sorted(results), [6, 7, 8, 9])

    def test_同時実行の後も窓が過ぎれば復帰する(self):  # V29
        clock = FakeClock()
        rl = RateLimiter(limit=2, window_sec=10, clock=clock)
        self._run_concurrently(rl, 5)
        rl.hook = lambda: None
        with self.assertRaises(RateLimited):
            rl.hit(1)
        clock.t += 10
        self.assertEqual(rl.hit(1), 1)
