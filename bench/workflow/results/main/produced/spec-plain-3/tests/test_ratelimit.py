import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class RateLimiterTest(unittest.TestCase):
    def test_limit_and_remaining(self):
        rl = RateLimiter(3, 10, Clock())
        self.assertEqual([rl.hit(1), rl.hit(1), rl.hit(1)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            rl.hit(1)
        self.assertEqual(rl.hit(2), 2)  # 利用者ごと

    def test_window_boundary(self):
        c = Clock()
        rl = RateLimiter(2, 10, c)
        rl.hit(1)
        rl.hit(1)
        c.t = 109.999
        with self.assertRaises(RateLimited):
            rl.hit(1)
        c.t = 110.0  # ちょうど window 秒で新しい枠
        self.assertEqual(rl.hit(1), 1)
        self.assertEqual(rl.hit(1), 0)
        with self.assertRaises(RateLimited):
            rl.hit(1)
        c.t = 119.9
        with self.assertRaises(RateLimited):
            rl.hit(1)
        c.t = 120.0
        rl.hit(1)

    def test_rejected_hits_not_counted_and_dont_extend(self):
        c = Clock()
        rl = RateLimiter(1, 10, c)
        rl.hit(1)
        c.t = 105
        with self.assertRaises(RateLimited):
            rl.hit(1)
        c.t = 110
        rl.hit(1)

    def test_concurrent_never_exceeds(self):
        limit, n = 5, 20
        rl = RateLimiter(limit, 60)
        gate = threading.Event()
        arrived = []
        lock = threading.Lock()

        def hook():  # 判定の後で全員をそろえる
            with lock:
                arrived.append(1)
                if len(arrived) >= n:
                    gate.set()
            gate.wait(timeout=5)
        rl.hook = hook
        ok, ng = [], []

        def work():
            try:
                rl.hit(1)
                ok.append(1)
            except RateLimited:
                ng.append(1)
        ts = [threading.Thread(target=work) for _ in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(ok), limit)
        self.assertEqual(len(ng), n - limit)

    def test_concurrent_plain(self):
        rl = RateLimiter(50, 60)
        ok = []

        def work():
            for _ in range(20):
                try:
                    rl.hit(7)
                    ok.append(1)
                except RateLimited:
                    pass
        ts = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(ok), 50)
