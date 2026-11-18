"""V17: レート制限の並行・再入（hook で判定と計数の間に割り込む）。"""
import threading
import unittest

from notes.errors import RateLimited
from notes.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _wait(barrier: threading.Barrier) -> None:
    barrier.wait()


def _noop() -> None:
    return None


def _run_hits(rl: RateLimiter, user: str, n: int) -> tuple[int, int]:
    ok = limited = 0
    for _ in range(n):
        try:
            rl.hit(user)
            ok += 1
        except RateLimited:
            limited += 1
    return ok, limited


class RateLimitConcurrencyTest(unittest.TestCase):
    def test_ratelimit_フック中に別スレッドが上限まで使うと外側は拒否される(self) -> None:
        rl = RateLimiter(limit=2, window_sec=60, clock=Clock())
        fired: list[int] = []
        result: list[tuple[int, int]] = []

        def hook() -> None:
            if fired:
                return
            fired.append(1)
            t = threading.Thread(target=lambda: result.append(_run_hits(rl, "u", 2)))
            t.start()
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "別スレッドの hit がデッドロックした")

        rl.hook = hook
        with self.assertRaises(RateLimited):
            rl.hit("u")  # 判定の時点では枠が空いていたが、計数の直前に再判定される
        self.assertEqual(result, [(2, 0)])
        rl.hook = _noop
        with self.assertRaises(RateLimited):
            rl.hit("u")

    def test_ratelimit_フック中の再入hitでも上限を超えない(self) -> None:
        rl = RateLimiter(limit=1, window_sec=60, clock=Clock())
        depth: list[int] = []

        def hook() -> None:
            if depth:
                return
            depth.append(1)
            self.assertEqual(rl.hit("u"), 0)  # 再入の hit が先に枠を使う

        rl.hook = hook
        with self.assertRaises(RateLimited):
            rl.hit("u")

    def test_ratelimit_全スレッドが判定を通過した後に数えても上限を超えない(self) -> None:
        n, limit = 8, 3
        rl = RateLimiter(limit=limit, window_sec=60, clock=Clock())
        barrier = threading.Barrier(n, timeout=10)
        rl.hook = lambda: _wait(barrier)  # 全員が判定を通過してから計数に進む
        outcomes: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                rl.hit("u")
                ok = True
            except RateLimited:
                ok = False
            with lock:
                outcomes.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(len(outcomes), n)
        self.assertEqual(sum(outcomes), limit)

    def test_ratelimit_素の並行呼び出しでも上限ちょうど(self) -> None:
        rl = RateLimiter(limit=5, window_sec=60, clock=Clock())
        results: list[tuple[int, int]] = []
        lock = threading.Lock()

        def worker() -> None:
            r = _run_hits(rl, "u", 10)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(sum(r[0] for r in results), 5)
        self.assertEqual(sum(r[1] for r in results), 75)

    def test_ratelimit_フック中に窓が回復する境界(self) -> None:
        clock = Clock()
        rl = RateLimiter(limit=1, window_sec=60, clock=clock)
        rl.hit("u")

        # 判定の時点では拒否される（フックは呼ばれない）
        calls: list[int] = []
        rl.hook = lambda: calls.append(1)
        with self.assertRaises(RateLimited):
            rl.hit("u")
        self.assertEqual(calls, [])
        clock.t += 60
        self.assertEqual(rl.hit("u"), 0)
        self.assertEqual(calls, [1])

        # フック中に時計が進み、他の利用者の枠は影響を受けない
        clock.t += 1
        rl.hook = lambda: setattr(clock, "t", clock.t + 1000)
        self.assertEqual(rl.hit("other"), 0)


if __name__ == "__main__":
    unittest.main()
