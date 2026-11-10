"""テスト共通のヘルパー。ダミーの利用者・DB だけを使う。"""
import os
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Callable
from typing import Any

from notes import db, service


def make_users(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        "alice": db.add_user(conn, 1, 1, "alice", "member", "alice-dummy-key-0001"),
        "bob": db.add_user(conn, 2, 1, "bob", "member", "bob-dummy-key-0002"),
        "admin": db.add_user(conn, 3, 1, "admin", "admin", "admin-dummy-key-0003"),
        "carol": db.add_user(conn, 4, 2, "carol", "member", "carol-dummy-key-0004"),
        "root2": db.add_user(conn, 5, 2, "root2", "admin", "root2-dummy-key-0005"),
    }


def snapshot(conn: sqlite3.Connection) -> tuple[Any, ...]:
    out = []
    for table in ("notes", "note_tags", "audit"):
        out.append(tuple(tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")))
    return tuple(out)


class DbTestCase(unittest.TestCase):
    """メモリ DB と利用者を用意し、hooks を必ず元に戻す。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.u = make_users(self.conn)
        self.addCleanup(self.reset_hook)

    @staticmethod
    def reset_hook() -> None:
        service.hooks.__dict__.pop("before_write", None)

    def set_hook(self, fn: Callable[[str], None]) -> None:
        service.hooks.before_write = fn  # type: ignore[assignment]

    def fail_at(self, step: str, exc: BaseException) -> None:
        def hook(s: str) -> None:
            if s == step:
                raise exc
        self.set_hook(hook)

    def note(self, owner: str = "alice", title: str = "題名", body: str = "本文です",
             tags: tuple[str, ...] = (), private: bool = False) -> int:
        return service.create_note(self.conn, self.u[owner], title, body, tags, private)


class FileDbTestCase(DbTestCase):
    """複数接続・複数スレッド用。ファイル DB を一時ディレクトリに作る。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "notes.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.u = make_users(self.conn)
        self.addCleanup(self.reset_hook)

    def new_conn(self) -> sqlite3.Connection:
        c = db.connect(self.path)
        self.addCleanup(c.close)
        return c


def run_threads(fns: list[Callable[[], None]]) -> list[BaseException]:
    """全関数を別スレッドで実行し、送出された例外を集めて返す。"""
    errors: list[BaseException] = []

    def wrap(fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=wrap, args=(f,)) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
        assert not t.is_alive(), "スレッドが終わりません"
    return errors
