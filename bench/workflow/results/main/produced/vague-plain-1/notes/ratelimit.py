import threading
import time
from collections import deque

from .errors import RateLimited, ValidationError


class RateLimiter:
    """ユーザーごとのスライディングウィンドウ式の制限。スレッドセーフ。"""

    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive int")
        if isinstance(window_sec, bool) or not isinstance(window_sec, (int, float)) or window_sec <= 0:
            raise ValidationError("window_sec must be positive")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = lambda: None
        self._lock = threading.Lock()
        self._hits = {}

    def _prune(self, user_id, now):
        q = self._hits.setdefault(user_id, deque())
        while q and q[0] <= now - self.window_sec:
            q.popleft()
        return q

    def hit(self, user_id):
        with self._lock:
            if len(self._prune(user_id, self.clock())) >= self.limit:
                raise RateLimited("rate limit exceeded")
        # テスト用の差し込み口。ロックの外で呼ぶ（別スレッドの hit を待てるように）
        self.hook()
        with self._lock:
            # 判定と加算を同じロックの中でやり直す（判定後に他の呼び出しが入っても超えない）
            now = self.clock()
            q = self._prune(user_id, now)
            if len(q) >= self.limit:
                raise RateLimited("rate limit exceeded")
            q.append(now)
            return self.limit - len(q)
