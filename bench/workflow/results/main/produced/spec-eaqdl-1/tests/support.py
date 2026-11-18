"""テスト共通部品。本番の DB は使わず、:memory: か一時フォルダのファイル DB だけを使う。"""
import unittest

from notes import db, service

# 本物らしくないダミーの鍵
KEY_ADMIN = "ADMINKEY-1234567890"
KEY_ALICE = "ALICEKEY-0000AAAA"
KEY_BOB = "BOBKEY-1111BBBB"
KEY_SHORT = "abc12"  # 8 文字未満


def make_db(path=":memory:"):
    return db.connect(path)


def make_users(conn):
    """組織1: alice(1)・bob(2)・admin(3)、組織2: carol(4)・dora(5, admin)。"""
    return {
        "alice": db.add_user(conn, 1, 1, "alice", "member", KEY_ALICE),
        "bob": db.add_user(conn, 2, 1, "bob", "member", KEY_BOB),
        "admin": db.add_user(conn, 3, 1, "admin1", "admin", KEY_ADMIN),
        "carol": db.add_user(conn, 4, 2, "carol", "member", "CAROLKEY-2222CCCC"),
        "dora": db.add_user(conn, 5, 2, "dora", "admin", "DORAKEY-3333DDDD"),
    }


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class BaseCase(unittest.TestCase):
    """hooks の差し替えを必ず元に戻す。"""

    def setUp(self):
        original = service.hooks.before_write
        self.addCleanup(lambda: setattr(service.hooks, "before_write", original))
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.u = make_users(self.conn)

    def set_hook(self, fn):
        service.hooks.before_write = fn

    def fail_at(self, step, exc=None):
        exc = exc or RuntimeError("boom")

        def hook(s):
            if s == step:
                raise exc

        self.set_hook(hook)
