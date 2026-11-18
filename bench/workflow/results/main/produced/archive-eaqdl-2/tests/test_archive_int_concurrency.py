"""AC12・V14: 競合。別接続・hooks・Barrier で決定論的に再現する（sleep に頼らない）。"""
import os
import tempfile
import threading

from notes import db, service
from notes.errors import Conflict

from .base import Base


class ArchiveConcurrencyTest(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.conn.close()
        self.conn = db.connect(self.path)
        for u in (self.alice, self.admin):
            db.add_user(self.conn, u["id"], u["org_id"], u["name"], u["role"], u["api_key"])
        self.nid = service.create_note(self.conn, self.alice, "t", "b")

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def stored(self):
        r = self.conn.execute("SELECT version, archived FROM notes WHERE id = ?", (self.nid,)).fetchone()
        return r["version"], r["archived"]

    def test_archive_interleaved_by_other_connection_archive(self):
        other = db.connect(self.path)
        done = []

        def interleave(step):
            if step == "archive" and not done:
                done.append(1)
                service.archive_note(other, self.admin, self.nid)  # 判定と書き込みの間に別接続が成功
        service.hooks.before_write = interleave
        with self.assertRaises(Conflict):
            service.archive_note(self.conn, self.alice, self.nid)
        other.close()
        self.assertEqual(self.stored(), (2, 1))  # version は1しか増えない
        self.assertEqual(sum(1 for a in service.export_audit(self.conn, self.admin) if a["action"] == "archive"), 1)

    def test_update_interleaved_by_other_connection_archive(self):
        other = db.connect(self.path)
        done = []

        def interleave(step):
            if step == "update" and not done:
                done.append(1)
                service.archive_note(other, self.admin, self.nid)
        service.hooks.before_write = interleave
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="new")
        other.close()
        row = self.conn.execute("SELECT title FROM notes WHERE id = ?", (self.nid,)).fetchone()
        self.assertEqual((row["title"], self.stored()), ("t", (2, 1)))

    def test_archive_interleaved_by_other_connection_update(self):
        other = db.connect(self.path)
        done = []

        def interleave(step):
            if step == "archive" and not done:
                done.append(1)
                service.update_note(other, self.alice, self.nid, 1, title="upd")
        service.hooks.before_write = interleave
        self.assertEqual(service.archive_note(self.conn, self.alice, self.nid), 3)  # 更新(v2)の後にアーカイブ(v3)
        other.close()
        self.assertEqual(self.stored(), (3, 1))

    def test_concurrent_archive_threads_only_one_wins(self):
        n = 6
        barrier = threading.Barrier(n)
        results = []
        lock = threading.Lock()

        def worker():
            c = db.connect(self.path)
            try:
                barrier.wait()
                try:
                    r = service.archive_note(c, self.alice, self.nid)
                except Conflict:
                    r = "conflict"
                with lock:
                    results.append(r)
            finally:
                c.close()
        ts = [threading.Thread(target=worker) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(map(str, results)), sorted(["2"] + ["conflict"] * (n - 1)))
        self.assertEqual(self.stored(), (2, 1))
        self.assertEqual(sum(1 for a in service.export_audit(self.conn, self.admin) if a["action"] == "archive"), 1)
