"""テスト共通: 接続・利用者・フック差し替え。DB は常にテスト用（:memory: か一時ファイル）。"""
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from notes import db, service
from notes.errors import NotesError

# テスト専用のダミー値（本番の値ではない）
BODY_SECRET = "ダミー本文-SECRET-BODY-xyz"
API_KEY_A = "dummy-api-key-ADMIN-1234"
API_KEY_B = "dummy-api-key-USER-5678"
SHORT_KEY = "abc"
BARRIER_TIMEOUT = 10


class BaseCase(unittest.TestCase):
    """:memory: の接続と、org1 の admin(1)・member(2)・member(3)、org2 の admin(10)・member(11) を用意する。"""

    def setUp(self) -> None:
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.admin = db.add_user(self.conn, 1, 1, "admin1", "admin", API_KEY_A)
        self.alice = db.add_user(self.conn, 2, 1, "alice", "member", API_KEY_B)
        self.bob = db.add_user(self.conn, 3, 1, "bob", "member", SHORT_KEY)
        self.admin2 = db.add_user(self.conn, 10, 2, "admin2", "admin", "other-org-admin-key-9999")
        self.carol = db.add_user(self.conn, 11, 2, "carol", "member", "other-org-member-key-0000")

    def set_hook(self, fn: Callable[[str], None]) -> None:
        """service.hooks.before_write を差し替え、終了時に必ず元へ戻す。"""
        self._orig_hook = service.hooks.before_write
        self.addCleanup(setattr, service.hooks, "before_write", self._orig_hook)
        service.hooks.before_write = fn  # type: ignore[method-assign]

    def restore_hook(self) -> None:
        service.hooks.before_write = self._orig_hook  # type: ignore[method-assign]

    def fail_at(self, step_name: str) -> None:
        def hook(step: str) -> None:
            if step == step_name:
                raise RuntimeError("意図した失敗")
        self.set_hook(hook)

    def counts(self) -> dict[str, int]:
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("notes", "note_tags", "audit")}

    def snapshot(self) -> list[tuple[Any, ...]]:
        rows = self.conn.execute("SELECT * FROM notes ORDER BY id").fetchall()
        return [tuple(r) for r in rows]

    def make(self, user: dict[str, Any], title: str = "題名", body: str = "本文です", tags: Any = (),
             private: bool = False) -> int:
        return service.create_note(self.conn, user, title, body, tags, private)

    def assert_reusable(self) -> None:
        """例外後も接続が次の操作に使える（V23/V24）。"""
        self.assertFalse(self.conn.in_transaction)
        nid = service.create_note(self.conn, self.alice, "再利用確認", "再利用確認の本文です")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["views"], 1)
        self.assertFalse(self.conn.in_transaction)


class FileDbCase(unittest.TestCase):
    """並行テスト用: 一時ディレクトリのファイル DB（テストごとに作って破棄）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "test.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.alice = db.add_user(self.conn, 2, 1, "alice", "member", API_KEY_B)
        self.bob = db.add_user(self.conn, 3, 1, "bob", "member", SHORT_KEY)

    def new_conn(self) -> sqlite3.Connection:
        c = db.connect(self.path)
        self.addCleanup(c.close)
        return c

    def new_conn_threadsafe(self) -> sqlite3.Connection:
        """スレッドの中で呼ぶ接続の作成（addCleanup を使わず、呼び出し側が close する）。"""
        return db.connect(self.path)

    def set_hook(self, fn: Callable[[str], None]) -> None:
        orig = service.hooks.before_write
        self.addCleanup(setattr, service.hooks, "before_write", orig)
        service.hooks.before_write = fn  # type: ignore[method-assign]


def run_threads(targets: list[Callable[[], Any]]) -> list[Any]:
    """各 target を別スレッドで実行し、結果（例外も値として）を順に返す。join は timeout 付き。"""
    results: list[Any] = [None] * len(targets)

    def wrap(i: int, fn: Callable[[], Any]) -> None:
        try:
            results[i] = fn()
        except BaseException as e:  # noqa: BLE001 主スレッドで検査するため集める
            results[i] = e

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(BARRIER_TIMEOUT * 2)
        if t.is_alive():
            raise AssertionError("スレッドが終了しない")
    return results


__all__ = ["BaseCase", "FileDbCase", "NotesError", "run_threads"]
