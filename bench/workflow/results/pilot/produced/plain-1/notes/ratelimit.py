import threading
import time

from .errors import RateLimited


class RateLimiter:
    """利用者ごとの固定枠の回数制限。枠の開始から window_sec 秒たつと新しい枠になる。"""

    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = lambda: None
        self._lock = threading.Lock()
        self._state = {}  # user_id -> [枠の開始, 回数]

    def _current(self, user_id, now):
        st = self._state.get(user_id)
        if st is None or now - st[0] >= self.window_sec:
            st = [now, 0]
            self._state[user_id] = st
        return st

    def hit(self, user_id):
        with self._lock:
            st = self._current(user_id, self.clock())
            if st[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
        # 判定の後・数える前。ロックは持たない（他のスレッドを止めない）ので、数える側で判定し直す
        self.hook()
        with self._lock:
            st = self._current(user_id, self.clock())
            if st[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
            st[1] += 1
            return self.limit - st[1]
