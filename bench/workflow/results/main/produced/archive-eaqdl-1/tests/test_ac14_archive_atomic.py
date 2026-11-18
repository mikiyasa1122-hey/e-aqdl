"""AC14・K10・K11: 監査ログの記録・途中失敗のロールバック・同時実行（ファイルDB・別接続）。"""
import sqlite3
import threading

from notes import db, service
from notes.errors import Conflict, Forbidden

from .filedb import FileDbBase


class AuditTest(FileDbBase):
    """V5: 監査ログの内容と、書き込みの途中失敗でのロールバック。"""

    def test_audit_record_on_success(self):
        for user in (self.alice, self.admin):
            service.archive_note(self.conn, user, self.nid)
            service.unarchive_note(self.conn, user, self.nid)
        rows = self.audits("archive")
        self.assertEqual([(r["detail"], r["actor_id"], r["org_id"]) for r in rows],
                         [(f"note={self.nid}", 1, 10), (f"note={self.nid}", 3, 10)])
        rows = self.audits("unarchive")
        self.assertEqual([(r["detail"], r["actor_id"]) for r in rows], [(f"note={self.nid}", 1), (f"note={self.nid}", 3)])

    def test_failure_before_audit_rolls_back_archive(self):
        before = self.audits()
        self.fail_at("archive:audit")
        with self.assertRaises(RuntimeError):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.state(), (1, 0))
        self.assertEqual(self.audits(), before)

    def test_failure_before_audit_rolls_back_unarchive(self):
        service.archive_note(self.conn, self.alice, self.nid)
        before = self.audits()
        self.fail_at("unarchive:audit")
        with self.assertRaises(RuntimeError):
            service.unarchive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.state(), (2, 1))
        self.assertEqual(self.audits(), before)

    def test_failure_in_audit_insert_rolls_back(self):
        # 監査ログの INSERT 自体が失敗しても archived・version は戻る
        self.conn.execute("CREATE TRIGGER no_audit BEFORE INSERT ON audit WHEN NEW.action = 'archive' "
                          "BEGIN SELECT RAISE(ABORT, 'audit denied'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertEqual(self.state(), (1, 0))
        self.assertEqual(self.audits("archive"), [])


class RollbackReuseTest(FileDbBase):
    """V6: 失敗の後に同じ接続を使い続けられる。"""

    def test_connection_usable_after_failure(self):
        self.fail_at("archive:audit")
        with self.assertRaises(RuntimeError):
            service.archive_note(self.conn, self.alice, self.nid)
        self.assertFalse(self.conn.in_transaction)
        service.hooks.before_write = lambda step: None
        self.assertEqual(service.archive_note(self.conn, self.alice, self.nid), 2)
        self.assertEqual(len(self.audits("archive")), 1)
        # 別接続からも、ロックが残らず更新できる
        other = db.connect(self.path)
        try:
            self.assertEqual(service.unarchive_note(other, self.alice, self.nid), 3)
        finally:
            other.close()


class ConcurrencyTest(FileDbBase):
    """V12: 別接続・別スレッドで同時に archive・archive と update。sleep は使わない。"""

    def run_threads(self, calls, steps):
        """各スレッドが指定の step に着いたところで全員が揃うまで待つ（確認を済ませてから書き込みに入る形を作る）。"""
        barrier = threading.Barrier(len(calls), timeout=10)

        def hook(step):
            if step in steps:
                barrier.wait()
        service.hooks.before_write = hook
        results = [None] * len(calls)

        def worker(i, call):
            conn = db.connect(self.path)
            try:
                results[i] = ("ok", call(conn))
            except (Conflict, RuntimeError) as e:  # 結果の型を比べる
                results[i] = ("err", e)
            finally:
                conn.close()
        threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(calls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
            self.assertFalse(t.is_alive())
        service.hooks.before_write = lambda step: None
        return results

    def test_two_concurrent_archives_one_wins(self):
        results = self.run_threads([lambda c: service.archive_note(c, self.alice, self.nid),
                                    lambda c: service.archive_note(c, self.admin, self.nid)], {"archive"})
        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(oks[0][1], 2)
        self.assertEqual(len(errs), 1)
        self.assertIsInstance(errs[0][1], Conflict)
        self.assertEqual(self.state(), (2, 1))
        self.assertEqual(len(self.audits("archive")), 1)

    def test_archive_and_update_concurrently(self):
        results = self.run_threads([lambda c: service.archive_note(c, self.alice, self.nid),
                                    lambda c: service.update_note(c, self.alice, self.nid, 1, body="new")],
                                   {"archive", "update"})
        archive_r, update_r = results
        self.assertEqual(archive_r[0], "ok")  # archive は必ず成功する（update は archived を変えない）
        version, archived = self.state()
        self.assertEqual(archived, 1)
        body = self.conn.execute("SELECT body FROM notes WHERE id = ?", (self.nid,)).fetchone()["body"]
        if update_r[0] == "ok":  # update が先: archive は version 2 → 3
            self.assertEqual((archive_r[1], update_r[1], version, body), (3, 2, 3, "new"))
        else:  # archive が先: update は Conflict で反映されない
            self.assertIsInstance(update_r[1], Conflict)
            self.assertEqual((archive_r[1], version, body), (2, 2, "b"))
        self.assertEqual(len(self.audits("archive")), 1)

    def test_archive_between_check_and_update_blocks_update(self):
        """update の確認の後・トランザクションの前に別接続でアーカイブされても、更新は反映されない。"""
        other = db.connect(self.path)

        def hook(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.archive_note(other, self.alice, self.nid)
        service.hooks.before_write = hook
        try:
            with self.assertRaises(Conflict):
                service.update_note(self.conn, self.alice, self.nid, 1, body="new")
        finally:
            other.close()
        self.assertEqual(self.state(), (2, 1))
        self.assertEqual(self.conn.execute("SELECT body FROM notes WHERE id = ?", (self.nid,)).fetchone()["body"], "b")
        self.assertEqual(len(self.audits("update")), 0)


class RecheckTest(FileDbBase):
    """K9: 最初の確認の後に持ち主が変わった場合、トランザクションの中の再判定で拒否する。"""

    def test_permission_is_rechecked_inside_transaction(self):
        bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        other = db.connect(self.path)

        def hook(step):
            if step == "archive":
                service.hooks.before_write = lambda s: None
                service.transfer_notes(other, self.admin, self.alice, bob)  # alice のメモが bob のものになる
        service.hooks.before_write = hook
        try:
            with self.assertRaises(Forbidden):
                service.archive_note(self.conn, self.alice, self.nid)
        finally:
            other.close()
        self.assertEqual(self.state(), (2, 0))  # transfer の version+1 だけ。archived は変わらない
        self.assertEqual(self.audits("archive"), [])
