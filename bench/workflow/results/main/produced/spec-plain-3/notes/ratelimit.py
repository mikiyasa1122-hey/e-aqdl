import threading
import time

from .errors import RateLimited


class RateLimiter:
    """利用者ごとの固定枠の回数制限。枠の開始からちょうど window_sec 秒で新しい枠になる。"""

    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = lambda: None
        self._lock = threading.Lock()
        self._state = {}  # user_id -> [枠の開始, 回数]

    def _remaining(self, user_id, now):
        """今の枠の残りを返す（枠が切れていれば満杯）。状態は変えない。"""
        st = self._state.get(user_id)
        if st is None or now - st[0] >= self.window_sec:
            return self.limit
        return self.limit - st[1]

    def hit(self, user_id):
        with self._lock:
            if self._remaining(user_id, self.clock()) <= 0:
                raise RateLimited("rate limit exceeded")
        # 判定の後・数える前（テストが同時の呼び出しを差し込む）。ロックは持たない
        self.hook()
        # 数える時にもう一度、判定と加算を不可分に行う（上限を超えて許さない）
        with self._lock:
            now = self.clock()
            if self._remaining(user_id, now) <= 0:
                raise RateLimited("rate limit exceeded")
            st = self._state.get(user_id)
            if st is None or now - st[0] >= self.window_sec:
                st = self._state[user_id] = [now, 0]
            st[1] += 1
            return self.limit - st[1]
