"""書き込み回数の制限（固定窓）。同時呼び出しでも上限を超えない。"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

from .errors import RateLimited, ValidationError


class RateLimiter:
    def __init__(self, limit: Any = 10, window_sec: Any = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit は 1 以上の整数で指定してください")
        if (isinstance(window_sec, bool) or not isinstance(window_sec, (int, float))
                or not math.isfinite(window_sec) or window_sec <= 0):
            raise ValidationError("window_sec は 0 より大きい数で指定してください")
        self.limit = limit
        self.window_sec = window_sec
        self._clock = clock
        self._lock = threading.Lock()
        # 利用者 id -> [窓の開始時刻, この窓での成功回数]
        self._state: dict[Any, list[Any]] = {}
        # 判定の後・計数の前に呼ばれる（テストが同時の呼び出しを再現する）
        self.hook: Callable[[], None] = lambda: None

    def _current(self, user_id: Any) -> list[Any]:
        """窓が過ぎていれば新しい窓にして、その状態を返す。ロック内で呼ぶ。"""
        now = self._clock()
        state = self._state.get(user_id)
        if state is None or now - state[0] >= self.window_sec:
            state = [now, 0]
            self._state[user_id] = state
        return state

    def hit(self, user_id: Any) -> int:
        """1 回分を数え、この窓の残り回数を返す。超えたら RateLimited。"""
        with self._lock:
            if self._current(user_id)[1] >= self.limit:
                raise RateLimited("書き込み回数の上限を超えました")
        # フックはロックの外で呼ぶ（Barrier で集めるテストがデッドロックしないように）
        self.hook()
        with self._lock:
            # 判定と加算を同じ臨界区間で行う。フックの間に他スレッドが進んでいても超えない
            state = self._current(user_id)
            if state[1] >= self.limit:
                raise RateLimited("書き込み回数の上限を超えました")
            state[1] += 1
            return self.limit - state[1]
