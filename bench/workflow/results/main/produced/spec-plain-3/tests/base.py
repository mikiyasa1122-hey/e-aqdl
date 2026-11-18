import os
import tempfile
import unittest

from notes import db, service


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.conn = db.connect(self.path)
        self.alice = db.add_user(self.conn, 1, 1, "alice", "member", "abcdefgh12345678")
        self.bob = db.add_user(self.conn, 2, 1, "bob", "member", "short")
        self.admin = db.add_user(self.conn, 3, 1, "root", "admin", "adminkey-9999")
        self.eve = db.add_user(self.conn, 4, 2, "eve", "member", "evekey")
        self.eadmin = db.add_user(self.conn, 5, 2, "eroot", "admin", "eadminkey")
        self._orig = service.hooks.before_write

    def tearDown(self):
        service.hooks.before_write = self._orig
        self.conn.close()
        self.dir.cleanup()

    def new_conn(self):
        c = db.connect(self.path)
        self.addCleanup(c.close)
        return c

    def count(self, table, where="1=1", args=()):
        return self.conn.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), args).fetchone()[0]
