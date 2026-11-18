"""固定枠のレート制限 (スレッドセーフ)。"""
import threading
import time

from .errors import RateLimited, ValidationError


def _noop():
    pass


class RateLimiter:
    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive int")
        if isinstance(window_sec, bool) or not isinstance(window_sec, (int, float)) or window_sec <= 0:
            raise ValidationError("window_sec must be positive")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = _noop
        self._lock = threading.Lock()
        self._state = {}  # user_id -> [枠の開始, 回数]

    def _current(self, user_id, now):
        st = self._state.get(user_id)
        if st is None or now - st[0] >= self.window_sec or now < st[0]:
            st = [now, 0]
            self._state[user_id] = st
        return st

    def hit(self, user_id):
        with self._lock:
            st = self._current(user_id, self.clock())
            if st[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
        self.hook()  # ロックの外 (hook が hit を呼んでもよい)
        with self._lock:
            now = self.clock()
            st = self._current(user_id, now)
            # hook の間に他の呼び出しが数えたかもしれないので、数える直前に判定し直す
            if st[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
            st[1] += 1
            if len(self._state) > 1000:
                self._state = {k: v for k, v in self._state.items()
                               if now - v[0] < self.window_sec}
            return self.limit - st[1]
