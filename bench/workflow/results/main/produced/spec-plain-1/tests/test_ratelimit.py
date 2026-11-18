import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class RateLimiterTests(unittest.TestCase):
    def test_limit_and_remaining(self):
        r = RateLimiter(3, 10, Clock())
        self.assertEqual([r.hit(1) for _ in range(3)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            r.hit(1)
        self.assertEqual(r.hit(2), 2)

    def test_window_boundary(self):
        c = Clock()
        r = RateLimiter(2, 10, c)
        r.hit(1)
        r.hit(1)
        c.t = 9.999
        with self.assertRaises(RateLimited):
            r.hit(1)
        c.t = 10
        self.assertEqual(r.hit(1), 1)

    def test_concurrent(self):
        r = RateLimiter(3, 60)
        barrier = threading.Barrier(6, timeout=5)
        r.hook = lambda: barrier.wait()
        results = []

        def work():
            try:
                r.hit(1)
                results.append("ok")
            except RateLimited:
                results.append("limited")
        ts = [threading.Thread(target=work) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("ok"), 3)
        self.assertEqual(results.count("limited"), 3)


if __name__ == "__main__":
    unittest.main()
