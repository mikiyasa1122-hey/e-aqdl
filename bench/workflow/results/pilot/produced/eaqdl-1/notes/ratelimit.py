"""利用者ごとの書き込み回数制限。"""
import threading
import time
from collections.abc import Callable

from notes.errors import RateLimited


class RateLimiter:
    def __init__(self, limit: int = 10, window_sec: float = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        self._lock = threading.Lock()
        self._state: dict[object, list[float]] = {}  # user_id -> [枠の開始時刻, 回数]
        self.hook: Callable[[], None] = lambda: None  # テストが同時の呼び出しを再現する場所

    def _judge(self, user_id: object) -> list[float]:
        """呼び出し側が _lock を持っている前提。枠を更新し、上限なら RateLimited。"""
        now = self.clock()
        st = self._state.get(user_id)
        if st is None or now - st[0] >= self.window_sec:
            st = [now, 0]
            self._state[user_id] = st
        if st[1] >= self.limit:
            raise RateLimited("書き込み回数の上限を超えました")
        return st

    def hit(self, user_id: object) -> int:
        with self._lock:
            self._judge(user_id)
        self.hook()  # ロックの外で呼ぶ
        with self._lock:
            st = self._judge(user_id)  # 再判定と加算を同じロック内で行う
            st[1] += 1
            return int(self.limit - st[1])
