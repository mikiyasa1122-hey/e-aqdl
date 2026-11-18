import threading
import time
from collections import deque

from .errors import RateLimited, ValidationError


def _noop():
    pass


class RateLimiter:
    """ユーザーごとの書き込み回数の制限（スライディングウィンドウ）。スレッドセーフ。"""

    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        if isinstance(window_sec, bool) or not isinstance(window_sec, (int, float)) or window_sec <= 0:
            raise ValidationError("window_sec must be positive")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = _noop
        self._lock = threading.Lock()
        self._hits = {}

    def _remaining(self, user_id, now):
        q = self._hits.get(user_id)
        if q is None:
            return self.limit
        while q and q[0] <= now - self.window_sec:
            q.popleft()
        if not q:
            del self._hits[user_id]
            return self.limit
        return self.limit - len(q)

    def hit(self, user_id):
        with self._lock:
            if self._remaining(user_id, self.clock()) <= 0:
                raise RateLimited("too many writes")
        # ロックの外で hook を呼ぶ（同時の呼び出しがここに割り込める）
        self.hook()
        # 判定と記録は1つのロックの中でやり直す（割り込みがあっても上限を超えない）
        with self._lock:
            now = self.clock()
            if self._remaining(user_id, now) <= 0:
                raise RateLimited("too many writes")
            self._hits.setdefault(user_id, deque()).append(now)
            return self.limit - len(self._hits[user_id])
