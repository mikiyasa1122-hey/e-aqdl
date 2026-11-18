"""利用者ごとの固定窓レート制限（スレッドセーフ）。"""
import threading
import time

from notes.errors import RateLimited, ValidationError


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class RateLimiter:
    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValidationError("limit must be an integer >= 1")
        if not _is_number(window_sec) or not window_sec > 0:
            raise ValidationError("window_sec must be > 0")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self.hook = lambda: None  # テストが同時の呼び出しを再現する差し込み口
        self._lock = threading.Lock()
        self._state = {}  # user_id -> [窓の開始, 回数]

    def _current(self, user_id):
        """ロックを持った状態で呼ぶ。切れた窓は捨てて、今の [開始, 回数] を返す（なければ None）。"""
        now = self.clock()
        entry = self._state.get(user_id)
        if entry is not None and now - entry[0] >= self.window_sec:
            entry = None
            self._state.pop(user_id, None)
        return now, entry

    def hit(self, user_id):
        # 1. 判定（ロック下）。拒否は数えない
        with self._lock:
            _, entry = self._current(user_id)
            if entry is not None and entry[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
        # 2. hook はロックの外で呼ぶ（hook 内の再入や他スレッドの待ちでデッドロックしない）
        self.hook()
        # 3. 再判定して数える（判定と加算は同じロック区間）
        with self._lock:
            now, entry = self._current(user_id)
            if entry is None:
                entry = [now, 0]
                self._state[user_id] = entry
            if entry[1] >= self.limit:
                raise RateLimited("rate limit exceeded")
            entry[1] += 1
            return self.limit - entry[1]
