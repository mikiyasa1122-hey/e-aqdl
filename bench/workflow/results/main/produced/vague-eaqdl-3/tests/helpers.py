"""テスト共通部品。DB は :memory: か一時ディレクトリ内のファイルだけを使う。"""
import unittest
from typing import Any

from notes import db, service


def fake_key(name: str) -> str:
    """テスト用のダミー api_key（実在の鍵ではない）。"""
    return f"secret-key-{name}-zq81x"


class NotesTestCase(unittest.TestCase):
    """hooks と _now を必ず元に戻す基底クラス。"""

    def setUp(self) -> None:
        self._orig_now = service._now
        self.addCleanup(self._restore)
        self.conn = self.open_db(":memory:")
        self.add_users(self.conn)

    def _restore(self) -> None:
        service.hooks.__dict__.pop("before_write", None)
        service._now = self._orig_now

    def open_db(self, path: str) -> Any:
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def add_users(self, conn: Any) -> None:
        """組織1: alice(member) bob(member) carol(admin)、組織2: dave(member) erin(admin)。"""
        self.alice = db.add_user(conn, 1, 1, "alice", "member", fake_key("alice"))
        self.bob = db.add_user(conn, 2, 1, "bob", "member", fake_key("bob"))
        self.carol = db.add_user(conn, 3, 1, "carol", "admin", fake_key("carol"))
        self.dave = db.add_user(conn, 4, 2, "dave", "member", fake_key("dave"))
        self.erin = db.add_user(conn, 5, 2, "erin", "admin", fake_key("erin"))

    def set_hook(self, fn: Any) -> None:
        service.hooks.before_write = fn  # type: ignore[method-assign]

    def snapshot(self, conn: Any = None) -> dict[str, list[tuple]]:
        """全テーブルの内容（比較用）。"""
        conn = conn or self.conn
        out = {}
        for t in ("users", "notes", "note_tags", "audit"):
            out[t] = [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
        return out
