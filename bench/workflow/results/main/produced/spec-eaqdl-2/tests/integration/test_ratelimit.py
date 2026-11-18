"""V17, V18: RateLimiter（偽 clock・hook によるスレッド同期）。"""
import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter
from tests.helpers import BARRIER_TIMEOUT, run_threads


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class WindowTest(unittest.TestCase):
    def test_limit回まで許可し残り回数を返し超過でRateLimited(self) -> None:
        rl = RateLimiter(limit=3, window_sec=10, clock=FakeClock())
        self.assertEqual([rl.hit("u") for _ in range(3)], [2, 1, 0])
        for _ in range(2):  # 拒否は数えず、何度でも拒否される
            with self.assertRaises(RateLimited):
                rl.hit("u")

    def test_枠の境界_1ティック前は同じ枠_ちょうどで新しい枠(self) -> None:
        clock = FakeClock(100.0)
        rl = RateLimiter(limit=2, window_sec=10, clock=clock)
        rl.hit("u")
        rl.hit("u")
        clock.t = 109.999
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 110.0  # 開始からちょうど window 秒
        self.assertEqual(rl.hit("u"), 1)
        self.assertEqual(rl.hit("u"), 0)
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 119.999  # 新しい枠の開始(110)から 1 ティック前
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 120.0
        self.assertEqual(rl.hit("u"), 1)

    def test_枠の開始は最初のhitの時刻(self) -> None:
        clock = FakeClock(0.0)
        rl = RateLimiter(limit=1, window_sec=5, clock=clock)
        rl.hit("u")
        clock.t = 4.0
        with self.assertRaises(RateLimited):
            rl.hit("u")
        clock.t = 5.0
        self.assertEqual(rl.hit("u"), 0)

    def test_利用者ごとに独立(self) -> None:
        rl = RateLimiter(limit=1, window_sec=10, clock=FakeClock())
        self.assertEqual(rl.hit("a"), 0)
        self.assertEqual(rl.hit("b"), 0)
        with self.assertRaises(RateLimited):
            rl.hit("a")
        with self.assertRaises(RateLimited):
            rl.hit("b")
        self.assertEqual(rl.hit(3), 0)  # 数値の id も可

    def test_既定値(self) -> None:
        rl = RateLimiter()
        self.assertEqual((rl.limit, rl.window_sec), (10, 60))
        self.assertEqual(rl.hit("u"), 9)

    def test_不正な設定はValueError(self) -> None:
        for kw in ({"limit": 0}, {"limit": -1}, {"window_sec": 0}, {"window_sec": -1}):
            with self.subTest(kw), self.assertRaises(ValueError):
                RateLimiter(**kw)  # type: ignore[arg-type]

    def test_hookは判定の後_数える前に呼ばれる(self) -> None:
        rl = RateLimiter(limit=1, window_sec=10, clock=FakeClock())
        seen: list[int] = []
        rl.hook = lambda: seen.append(len(rl._state.get("u", [0, 0])) and int(rl._state["u"][1]))
        rl.hit("u")
        self.assertEqual(seen, [0])  # hook 時点では未加算
        with self.assertRaises(RateLimited):
            rl.hit("u")
        self.assertEqual(seen, [0])  # 上限で拒否されるときは hook の前に拒否

    def test_hook中に枠が切り替わっても正しく数える(self) -> None:
        clock = FakeClock(0.0)
        rl = RateLimiter(limit=1, window_sec=10, clock=clock)
        rl.hit("u")
        clock.t = 10.0
        rl.hook = lambda: setattr(clock, "t", 20.0)  # 判定後に時間が進む
        self.assertEqual(rl.hit("u"), 0)  # 新しい枠の 1 回目
        rl.hook = lambda: None
        with self.assertRaises(RateLimited):
            rl.hit("u")


class ConcurrentTest(unittest.TestCase):
    def _run(self, n: int, limit: int, users: list[str]) -> dict[str, list[object]]:
        rl = RateLimiter(limit=limit, window_sec=1000, clock=FakeClock())
        barrier = threading.Barrier(n * len(users), timeout=BARRIER_TIMEOUT)
        rl.hook = lambda: barrier.wait()  # 全スレッドが判定を終えてから数え始める

        def worker(u: str):
            return lambda: rl.hit(u)
        targets = [worker(u) for u in users for _ in range(n)]
        results = run_threads(targets)
        out: dict[str, list[object]] = {u: [] for u in users}
        for i, r in enumerate(results):
            out[users[i // n]].append(r)
        return out

    def test_同時hitでも許可はlimit以下(self) -> None:
        n, limit = 12, 3
        out = self._run(n, limit, ["u"])["u"]
        allowed = [r for r in out if isinstance(r, int)]
        denied = [r for r in out if isinstance(r, RateLimited)]
        self.assertEqual(len(allowed), limit)
        self.assertEqual(len(denied), n - limit)
        self.assertEqual(sorted(allowed), list(range(limit)))  # 残り回数 0..limit-1 が重複なく

    def test_同時でも利用者ごとに独立(self) -> None:
        out = self._run(5, 2, ["a", "b"])
        for u in ("a", "b"):
            allowed = [r for r in out[u] if isinstance(r, int)]
            self.assertEqual(sorted(allowed), [0, 1])
            self.assertEqual(len([r for r in out[u] if isinstance(r, RateLimited)]), 3)

    def test_枠より少ない同時呼び出しは全て許可(self) -> None:
        out = self._run(3, 5, ["u"])["u"]
        self.assertEqual(sorted(out), [2, 3, 4])  # type: ignore[type-var]
