"""利用者ごとの書き込み回数制限。"""
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
        self._state = {}  # user_id -> [枠の開始時刻, 回数]

    def _current(self, user_id):
        # ロックを持った状態で呼ぶ。枠を更新して状態を返し、上限なら RateLimited
        now = self.clock()
        st = self._state.get(user_id)
        if st is None or now - st[0] >= self.window_sec:
            st = [now, 0]
            self._state[user_id] = st
        if st[1] >= self.limit:
            raise RateLimited("書き込み回数の上限を超えました")
        return st

    def hit(self, user_id):
        with self._lock:
            self._current(user_id)
        # ロックの外で呼ぶ（フック内で別スレッドの hit を待ってもデッドロックしない）
        self.hook()
        with self._lock:
            st = self._current(user_id)  # 割り込みに備えて再判定する
            st[1] += 1
            return self.limit - st[1]
