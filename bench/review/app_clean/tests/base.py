"""テストの共通部分。接続はテストごとに新しい（メモリ上の）DB。"""
import unittest

from notes import db, service


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.alice = db.add_user(self.conn, 1, 10, "alice", api_key="ak-alice-0001")
        self.bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        self.admin = db.add_user(self.conn, 3, 10, "root", role="admin", api_key="ak-root-0003")
        self.eve = db.add_user(self.conn, 9, 99, "eve", api_key="ak-eve-0009")
        self.eve_admin = db.add_user(self.conn, 8, 99, "evil-root", role="admin", api_key="ak-eroot-0008")
        service.hooks.before_write = lambda step: None

    def tearDown(self):
        service.hooks.before_write = lambda step: None
        self.conn.close()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def fail_at(self, wanted):
        """hooks の指定の段階で例外を起こす（途中の失敗の再現）。"""
        def boom(step):
            if step == wanted:
                raise RuntimeError(f"injected failure at {step}")
        service.hooks.before_write = boom
