"""V16: レート制限。"""
import unittest

from notes.errors import RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class RateLimitTest(unittest.TestCase):
    def test_ratelimit_上限までは残り回数を返し超過でRateLimited(self) -> None:
        rl = RateLimiter(limit=3, window_sec=60, clock=Clock())
        self.assertEqual([rl.hit("u"), rl.hit("u"), rl.hit("u")], [2, 1, 0])
        with self.assertRaises(RateLimited):
            rl.hit("u")
        with self.assertRaises(RateLimited):
            rl.hit("u")

    def test_ratelimit_窓経過で回復し境界ちょうどで回復(self) -> None:
        clock = Clock()
        rl = RateLimiter(limit=2, window_sec=60, clock=clock)
        rl.hit("u")
        clock.t += 30
        rl.hit("u")
        clock.t += 29.9  # 最初の打刻から 59.9 秒
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t += 0.1  # 最初の打刻からちょうど 60 秒で回復
        self.assertEqual(rl.hit("u"), 0)
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t += 100
        self.assertEqual(rl.hit("u"), 1)

    def test_ratelimit_利用者ごとに独立(self) -> None:
        rl = RateLimiter(limit=1, window_sec=60, clock=Clock())
        rl.hit("a")
        self.assertEqual(rl.hit("b"), 0)
        with self.assertRaises(RateLimited):
            rl.hit("a")

    def test_ratelimit_時計が戻っても上限を超えない(self) -> None:
        clock = Clock()
        rl = RateLimiter(limit=2, window_sec=60, clock=clock)
        rl.hit("u")
        rl.hit("u")
        clock.t -= 500  # 時計の逆行
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t += 59
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t += 1  # 逆行後の時刻から 60 秒
        self.assertEqual(rl.hit("u"), 1)

    def test_ratelimit_拒否された呼び出しは数えない(self) -> None:
        clock = Clock()
        rl = RateLimiter(limit=1, window_sec=10, clock=clock)
        rl.hit("u")
        clock.t += 5
        for _ in range(5):
            with self.assertRaises(RateLimited):
                rl.hit("u")
        clock.t += 5  # 最初の 1 回だけが対象なので、ここで回復する
        self.assertEqual(rl.hit("u"), 0)

    def test_ratelimit_既定値と不正な設定(self) -> None:
        rl = RateLimiter()
        self.assertEqual((rl.limit, rl.window_sec), (10, 60))
        for kwargs in ({"limit": 0}, {"limit": -1}, {"limit": 1.5}, {"limit": True},
                       {"window_sec": 0}, {"window_sec": -1}, {"window_sec": "60"}):
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                RateLimiter(**kwargs)
