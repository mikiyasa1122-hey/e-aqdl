"""利用者ごとの書き込み回数の制限（固定の時間枠）。複数のスレッドから同時に呼ばれる。"""
import threading
import time

from .errors import RateLimited


class RateLimiter:
    def __init__(self, limit=10, window_sec=60, clock=time.monotonic):
        self.limit = limit
        self.window = window_sec
        self.clock = clock
        self._lock = threading.Lock()
        self._counts = {}  # user_id -> (枠の開始時刻, 回数)
        self.hook = lambda: None  # テストから並行実行を再現するための差し込み口

    def hit(self, user_id):
        """1回分を数える。枠の中で limit 回を超えたら RateLimited。"""
        with self._lock:
            now = self.clock()
            start, n = self._counts.get(user_id, (now, 0))
            if now - start >= self.window:
                start, n = now, 0
            self.hook()
            if n >= self.limit:
                raise RateLimited(f"user {user_id}: {self.window}秒に{self.limit}回まで")
            self._counts[user_id] = (start, n + 1)
            return self.limit - n - 1
