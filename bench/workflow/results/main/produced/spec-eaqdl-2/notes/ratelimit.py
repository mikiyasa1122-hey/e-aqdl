"""利用者ごとの書き込み回数制限（固定枠）。"""
import threading
import time
from collections.abc import Callable

from .errors import RateLimited


class RateLimiter:
    def __init__(self, limit: int = 10, window_sec: float = 60, clock: Callable[[], float] = time.monotonic):
        if limit < 1 or window_sec <= 0:
            raise ValueError("limit は1以上、window_sec は正の数で指定してください")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        # テストが同時呼び出しを再現する差し込み口（枠の判定後・数える前に呼ぶ）
        self.hook: Callable[[], None] = lambda: None
        self._state: dict[object, list[float]] = {}  # 利用者 -> [枠の開始時刻, 回数]
        self._lock = threading.Lock()

    def _count_now(self, user_id: object) -> tuple[list[float], float]:
        """（ロック内で呼ぶ）現在の枠を返す。期限切れ・未作成なら新しい枠（回数0）を作る。"""
        now = self.clock()
        slot = self._state.get(user_id)
        if slot is None or now - slot[0] >= self.window_sec:
            slot = [now, 0]
            self._state[user_id] = slot
        return slot, now

    def hit(self, user_id: object) -> int:
        with self._lock:
            slot, _ = self._count_now(user_id)
            if slot[1] >= self.limit:
                raise RateLimited("書き込み回数の上限を超えました")
        self.hook()  # ロックの外で呼ぶ（テストが全スレッドを同期してもデッドロックしない）
        with self._lock:
            slot, _ = self._count_now(user_id)  # 判定を再実行してから数える
            if slot[1] >= self.limit:
                raise RateLimited("書き込み回数の上限を超えました")
            slot[1] += 1
            return self.limit - int(slot[1])
