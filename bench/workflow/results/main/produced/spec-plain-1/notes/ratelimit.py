"""利用者ごとの書き込み回数の制限（固定枠）。"""
import threading
import time

from .errors import RateLimited


class RateLimiter:
    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = lambda: None
        self._lock = threading.Lock()
        self._state = {}   # user_id -> [枠の開始, 回数]

    def _current(self, user_id, now):
        st = self._state.get(user_id)
        if st is None or now - st[0] >= self.window_sec:
            st = self._state[user_id] = [now, 0]
        return st

    def hit(self, user_id):
        with self._lock:
            if self._current(user_id, self.clock())[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
        self.hook()   # ロックの外で呼ぶ（同時の呼び出しを再現できるように）
        with self._lock:
            # 判定と加算を1つのロックで行い直す。hook の間に埋まっていたら拒否する
            st = self._current(user_id, self.clock())
            if st[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
            st[1] += 1
            return self.limit - st[1]
