"""テスト共通の補助。DB はメモリか一時ファイルのみ。実在の資格情報は使わない。"""
import contextlib
import os
import tempfile
import unittest
from collections.abc import Callable, Iterator
from typing import Any

from notes import db, service

# 組織1: alice(member, 作成者想定) / bob(member) / root1(admin)、組織2: carol(member) / root2(admin)
ALICE, BOB, ROOT1, CAROL, ROOT2 = 1, 2, 3, 4, 5


def setup_users(conn: Any) -> dict[str, dict[str, Any]]:
    return {
        "alice": db.add_user(conn, ALICE, 1, "alice", "member", "test-key-alice-0001"),
        "bob": db.add_user(conn, BOB, 1, "bob", "member", "test-key-bob-0002"),
        "root1": db.add_user(conn, ROOT1, 1, "root1", "admin", "test-key-root1-0003"),
        "carol": db.add_user(conn, CAROL, 2, "carol", "member", "test-key-carol-0004"),
        "root2": db.add_user(conn, ROOT2, 2, "root2", "admin", "test-key-root2-0005"),
    }


def counts(conn: Any) -> dict[str, int]:
    return {t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in ("notes", "note_tags", "audit")}


def snapshot(conn: Any) -> list[Any]:
    """全表の内容（変化の有無の比較用）。"""
    out: list[Any] = []
    for t in ("users", "notes", "note_tags", "audit"):
        out.append([tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")])
    return out


@contextlib.contextmanager
def hook(fn: Callable[[str], None]) -> Iterator[None]:
    """service.hooks.before_write を一時的に差し替える。"""
    service.hooks.before_write = fn  # type: ignore[assignment, method-assign]
    try:
        yield
    finally:
        service.hooks.__dict__.pop("before_write", None)


class MemCase(unittest.TestCase):
    """メモリ DB と利用者を用意する基底（テストメソッドを持たない）。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.u = setup_users(self.conn)
        self.alice = self.u["alice"]
        self.bob = self.u["bob"]
        self.root1 = self.u["root1"]
        self.carol = self.u["carol"]
        self.root2 = self.u["root2"]

    def note(self, user: Any = None, title: str = "題名", body: str = "本文", tags: Any = (),
             private: bool = False) -> int:
        return service.create_note(self.conn, user or self.alice, title, body, tags, private)

    def assertNoRows(self) -> None:
        self.assertEqual(counts(self.conn), {"notes": 0, "note_tags": 0, "audit": 0})


class FileCase(unittest.TestCase):
    """一時ファイル DB に 2 本の接続（c1 と c2）を張る基底。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "notes.db")
        self.c1 = db.connect(self.path)
        self.c2 = db.connect(self.path)
        self.addCleanup(self.c2.close)
        self.addCleanup(self.c1.close)
        self.u = setup_users(self.c1)
        self.alice = self.u["alice"]
        self.bob = self.u["bob"]
        self.root1 = self.u["root1"]
