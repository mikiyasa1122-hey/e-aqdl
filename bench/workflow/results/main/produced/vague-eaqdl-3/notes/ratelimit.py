"""書き込み回数の制限（スライディングウィンドウ）。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from notes.errors import RateLimited, ValidationError


def _noop() -> None:
    return None


class RateLimiter:
    def __init__(self, limit: int = 10, window_sec: float = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit は 1 以上の整数で指定してください")
        if isinstance(window_sec, bool) or not isinstance(window_sec, (int, float)) or not window_sec > 0:
            raise ValidationError("window_sec は正の数で指定してください")
        self.limit = limit
        self.window_sec = window_sec
        self.clock = clock
        # 判定の後・計数の前に呼ばれる（テストが同時の呼び出しを再現する）
        self.hook: Callable[[], None] = _noop
        self._lock = threading.Lock()
        self._stamps: dict[Any, list[float]] = {}

    def _prune(self, user_id: Any, now: float) -> list[float]:
        """期限切れの打刻を除く。時計が戻った場合、未来の打刻は now に丸める。"""
        stamps = [min(t, now) for t in self._stamps.get(user_id, [])]
        stamps = [t for t in stamps if now - t < self.window_sec]
        self._stamps[user_id] = stamps
        return stamps

    def hit(self, user_id: Any) -> int:
        """1 回数える。戻り値はこの枠の残りの回数。上限を超えるなら RateLimited。"""
        with self._lock:
            if len(self._prune(user_id, self.clock())) >= self.limit:
                raise RateLimited("書き込みが多すぎます")
        # ロックを保持せずに呼ぶ（別スレッド・再入の hit でデッドロックしない）
        self.hook()
        with self._lock:
            now = self.clock()
            stamps = self._prune(user_id, now)
            # フック中の割り込みがあっても超えないよう、計数の直前に再判定する
            if len(stamps) >= self.limit:
                raise RateLimited("書き込みが多すぎます")
            stamps.append(now)
            return self.limit - len(stamps)
