"""利用者ごとの固定枠の書き込み回数制限。"""
import threading
import time
from collections.abc import Callable

from notes.errors import RateLimited, ValidationError


def _noop() -> None:
    return None


class RateLimiter:
    def __init__(self, limit: int = 10, window_sec: float = 60, clock: Callable[[], float] = time.monotonic):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("invalid limit")
        if isinstance(window_sec, bool) or not isinstance(window_sec, (int, float)) or window_sec <= 0:
            raise ValidationError("invalid window")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        # 判定から加算の間に呼ばれる。テストが同時の呼び出しを再現するための差し込み口
        self.hook: Callable[[], None] = _noop
        self._lock = threading.Lock()
        self._state: dict = {}

    def _check(self, user_id) -> int:
        """ロックを持った状態で呼ぶ。超過なら RateLimited。現在の枠の count を返す。"""
        now = self.clock()
        state = self._state.get(user_id)
        if state is None or now - state[0] >= self.window_sec:
            return 0
        if state[1] >= self.limit:
            raise RateLimited("rate limit exceeded")
        return state[1]

    def hit(self, user_id) -> int:
        with self._lock:
            self._check(user_id)
        # ロックの外で呼ぶ（hook から別スレッドの hit を呼んでもデッドロックしない）
        self.hook()
        with self._lock:
            # 判定と加算を 1 つの臨界区間で行い、割り込みがあっても上限を超えない
            count = self._check(user_id)
            now = self.clock()
            state = self._state.get(user_id)
            if count == 0 and (state is None or now - state[0] >= self.window_sec):
                self._state[user_id] = [now, 1]
            else:
                state[1] += 1  # type: ignore[index]
            return self.limit - self._state[user_id][1]
