"""AC7: R7 の規則どおりに制限される（枠の境界・同時の呼び出しを含む）。"""
import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        self.t = [100.0]
        self.rl = RateLimiter(limit=2, window_sec=60, clock=lambda: self.t[0])

    def test_limit_within_window(self):
        self.assertEqual(self.rl.hit(1), 1)
        self.assertEqual(self.rl.hit(1), 0)
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.rl.hit(2)  # 利用者ごと

    def test_window_boundary(self):
        self.rl.hit(1)
        self.rl.hit(1)
        self.t[0] = 159.999
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.t[0] = 160.0  # 枠の開始からちょうど window 秒で新しい枠
        self.assertEqual(self.rl.hit(1), 1)

    def test_rejected_calls_do_not_extend_the_window(self):
        self.rl.hit(1)
        self.rl.hit(1)
        self.t[0] = 130
        with self.assertRaises(RateLimited):
            self.rl.hit(1)
        self.t[0] = 160
        self.rl.hit(1)

    def test_concurrent_calls_never_exceed_limit(self):
        rl = RateLimiter(limit=5, window_sec=60, clock=lambda: 0.0)
        barrier = threading.Barrier(20)
        ok = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                rl.hit(1)
                with lock:
                    ok.append(1)
            except RateLimited:
                pass
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        self.assertEqual(len(ok), 5)

    def test_interleaving_inside_the_check_is_serialized(self):
        rl = RateLimiter(limit=1, window_sec=60, clock=lambda: 0.0)
        a_in, b_done = threading.Event(), threading.Event()
        results = []

        def hook():
            if threading.current_thread().name == "A":
                a_in.set()
                b_done.wait(0.5)
        rl.hook = hook

        def run(name):
            try:
                rl.hit(1)
                results.append("ok")
            except RateLimited:
                results.append("limited")
            if name == "B":
                b_done.set()
        ta = threading.Thread(target=run, args=("A",), name="A")
        ta.start()
        a_in.wait(1)
        tb = threading.Thread(target=run, args=("B",), name="B")
        tb.start()
        ta.join(2)
        tb.join(2)
        self.assertEqual(sorted(results), ["limited", "ok"])
