"""V7 / V21: R7・K11 RateLimiter（枠の境界・同時呼び出し）。"""
import threading
import unittest

from notes.errors import RateLimited, ValidationError
from notes.ratelimit import RateLimiter

TIMEOUT = 30


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class RateLimitTest(unittest.TestCase):
    def test_integration_ratelimit_limit回まで成功し残りが減り超過で拒否(self):
        rl = RateLimiter(limit=3, window_sec=10, clock=FakeClock())
        self.assertEqual([rl.hit("u") for _ in range(3)], [2, 1, 0])
        with self.assertRaises(RateLimited):
            rl.hit("u")

    def test_integration_ratelimit_枠の境界(self):
        clock = FakeClock(1000)
        rl = RateLimiter(limit=2, window_sec=10, clock=clock)
        rl.hit("u")
        rl.hit("u")
        clock.t = 1009.999
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 1010  # ちょうど window 秒で新しい枠
        self.assertEqual(rl.hit("u"), 1)
        self.assertEqual(rl.hit("u"), 0)
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 1019
        with self.assertRaises(RateLimited):
            rl.hit("u")  # 新しい枠の開始は 1010
        clock.t = 1020
        self.assertEqual(rl.hit("u"), 1)

    def test_integration_ratelimit_拒否は数えず利用者ごとに独立(self):
        clock = FakeClock()
        rl = RateLimiter(limit=1, window_sec=5, clock=clock)
        self.assertEqual(rl.hit("a"), 0)
        for _ in range(5):
            with self.assertRaises(RateLimited):
                rl.hit("a")
        self.assertEqual(rl.hit("b"), 0)  # 別利用者は影響を受けない
        clock.t += 5
        self.assertEqual(rl.hit("a"), 0)  # 拒否が数えられていれば枠がずれる

    def test_integration_ratelimit_コンストラクタの検査(self):
        for kw in ({"limit": 0}, {"limit": -1}, {"limit": True}, {"window_sec": 0}, {"window_sec": -1}):
            with self.assertRaises(ValidationError, msg=repr(kw)):
                RateLimiter(**kw)

    def test_integration_ratelimit_hookの中から別スレッドのhitを呼んでもデッドロックしない(self):
        rl = RateLimiter(limit=2, window_sec=10, clock=FakeClock())
        result: list = []
        called = []

        def hook():
            if called:
                return
            called.append(True)
            t = threading.Thread(target=lambda: result.append(rl.hit("u")))
            t.start()
            t.join(timeout=TIMEOUT)
            result.append("alive" if t.is_alive() else "done")

        rl.hook = hook
        outer = rl.hit("u")
        self.assertEqual(result, [1, "done"])
        self.assertEqual(outer, 0)
        with self.assertRaises(RateLimited):
            rl.hit("u")

    def test_integration_ratelimit_10スレッドの同時呼び出しでも上限を超えない(self):
        n = 10
        rl = RateLimiter(limit=3, window_sec=100, clock=FakeClock())
        barrier = threading.Barrier(n, timeout=TIMEOUT)
        rl.hook = barrier.wait  # 全員が判定を終えて hook に揃ってから加算に進む
        outcomes: list = []
        lock = threading.Lock()

        def work():
            try:
                r = rl.hit("u")
            except RateLimited:
                r = "limited"
            except BaseException as e:  # noqa: BLE001
                r = e
            with lock:
                outcomes.append(r)

        threads = [threading.Thread(target=work) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)
            self.assertFalse(t.is_alive())
        ok = [o for o in outcomes if isinstance(o, int)]
        self.assertEqual(len(outcomes), n)
        self.assertEqual(sorted(ok), [0, 1, 2])
        self.assertEqual(outcomes.count("limited"), n - 3)

    def test_integration_ratelimit_枠境界をまたぐ同時呼び出し(self):
        clock = FakeClock(1000)
        rl = RateLimiter(limit=1, window_sec=10, clock=clock)
        rl.hit("u")
        clock.t = 1009  # まだ前の枠。拒否される
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 1010  # 新しい枠。2 スレッドが両方とも最初の判定を通っても成功は 1 回だけ
        barrier = threading.Barrier(2, timeout=TIMEOUT)
        rl.hook = barrier.wait
        outcomes: list = []

        def work():
            try:
                outcomes.append(rl.hit("u"))
            except RateLimited:
                outcomes.append("limited")

        threads = [threading.Thread(target=work) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(map(str, outcomes)), ["0", "limited"])

    def test_integration_ratelimit_hookの例外では数えない(self):
        rl = RateLimiter(limit=1, window_sec=10, clock=FakeClock())

        def boom():
            raise RuntimeError("hook失敗")

        rl.hook = boom
        with self.assertRaises(RuntimeError):
            rl.hit("u")
        rl.hook = lambda: None
        self.assertEqual(rl.hit("u"), 0)  # 失敗した呼び出しは数えられていない
