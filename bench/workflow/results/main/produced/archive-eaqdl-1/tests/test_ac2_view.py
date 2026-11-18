"""AC2・K1・K3: R2 の規則どおりに見え方が決まり、閲覧数が正確に増える。"""
import os
import tempfile
import threading

from notes import db, service
from notes.errors import NotFound

from .base import Base


class VisibilityTest(Base):
    def test_same_org_can_read_public(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        self.assertEqual(service.get_note(self.conn, self.bob, nid)["title"], "t")
        self.assertEqual(service.get_note(self.conn, self.admin, nid)["title"], "t")

    def test_hidden_notes_are_all_not_found(self):
        pub = service.create_note(self.conn, self.alice, "pub", "b")
        priv = service.create_note(self.conn, self.alice, "priv", "b", private=True)
        cases = [(self.eve, pub), (self.eve_admin, pub), (self.bob, priv), (self.admin, priv), (self.alice, 999)]
        for user, nid in cases:
            with self.subTest(user=user["name"], nid=nid), self.assertRaises(NotFound):
                service.get_note(self.conn, user, nid)
        self.assertEqual(service.get_note(self.conn, self.alice, priv)["title"], "priv")

    def test_not_found_does_not_count_a_view(self):
        nid = service.create_note(self.conn, self.alice, "t", "b", private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)

    def test_hidden_after_check_is_not_found(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def make_private(step):
            if step == "view":
                self.conn.execute("UPDATE notes SET private = 1 WHERE id = ?", (nid,))
        service.hooks.before_write = make_private
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
        self.assertEqual(self.conn.execute("SELECT views FROM notes").fetchone()[0], 0)


class ViewCountTest(Base):
    def test_each_view_counts_once(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        for i in range(1, 4):
            self.assertEqual(service.get_note(self.conn, self.bob, nid)["views"], i)

    def test_interleaved_view_is_not_lost(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")
        done = []

        def other(step):
            if step == "view" and not done:
                done.append(1)
                service.get_note(self.conn, self.bob, nid)
        service.hooks.before_write = other
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["views"], 2)

    def test_concurrent_views_from_threads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notes.db")
            conn = db.connect(path)
            alice = db.add_user(conn, 1, 10, "alice", api_key="ak-alice-0001")
            nid = service.create_note(conn, alice, "t", "b")
            errors = []

            def worker():
                c = db.connect(path)  # 接続はスレッドごとに1つ
                try:
                    for _ in range(10):
                        service.get_note(c, alice, nid)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                finally:
                    c.close()
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(errors, [])
            self.assertEqual(conn.execute("SELECT views FROM notes WHERE id = ?", (nid,)).fetchone()[0], 40)
            conn.close()
