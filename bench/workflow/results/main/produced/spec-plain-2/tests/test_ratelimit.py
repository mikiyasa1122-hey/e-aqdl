import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class RateLimiterTests(unittest.TestCase):
    def test_limit_and_remaining(self):
        rl = RateLimiter(limit=3, window_sec=10, clock=Clock())
        self.assertEqual([rl.hit(1) for _ in range(3)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            rl.hit(1)
        self.assertEqual(rl.hit(2), 2)  # 利用者ごと

    def test_window_boundary(self):
        c = Clock()
        rl = RateLimiter(limit=1, window_sec=10, clock=c)
        rl.hit(1)
        c.t += 9.999
        with self.assertRaises(RateLimited):
            rl.hit(1)
        c.t = 110.0  # ちょうど window 秒
        self.assertEqual(rl.hit(1), 0)
        with self.assertRaises(RateLimited):
            rl.hit(1)

    def test_concurrent_never_exceeds(self):
        rl = RateLimiter(limit=3, window_sec=60, clock=Clock())
        n = 8
        barrier = threading.Barrier(n)
        rl.hook = lambda: barrier.wait(timeout=5)
        out = []

        def work():
            try:
                rl.hit(1)
                out.append("ok")
            except RateLimited:
                out.append("limited")
            except threading.BrokenBarrierError:
                out.append("broken")

        ts = [threading.Thread(target=work) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(out.count("ok"), 3)
        self.assertNotIn("broken", out)

    def test_concurrent_real(self):
        rl = RateLimiter(limit=50, window_sec=60)
        out = []

        def work():
            for _ in range(20):
                try:
                    rl.hit(1)
                    out.append(1)
                except RateLimited:
                    pass

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(out), 50)


if __name__ == "__main__":
    unittest.main()
