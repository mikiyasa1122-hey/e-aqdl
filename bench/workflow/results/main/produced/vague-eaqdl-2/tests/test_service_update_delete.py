"""AC8 AC9 AC10: 更新・削除と版管理。"""
from unittest import mock

from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import MemCase, counts, hook, snapshot


def audit_rows(conn):
    return [tuple(r) for r in conn.execute("SELECT actor_id, action, detail FROM audit ORDER BY id")]


class UpdateDeleteUnit(MemCase):
    def test_作成者と管理者は更新できて他メンバーはForbidden(self):  # V8
        nid = self.note(self.alice, tags=["a"])
        v = service.update_note(self.conn, self.alice, nid, 1, title="新1")
        self.assertEqual(v, 2)
        v = service.update_note(self.conn, self.root1, nid, 2, body="新2")
        self.assertEqual(v, 3)
        before = snapshot(self.conn)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 3, title="乗っ取り")
        self.assertEqual(snapshot(self.conn), before)
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((got["title"], got["body"], got["version"]), ("新1", "新2", 3))
        self.assertEqual(audit_rows(self.conn)[1:], [(1, "update", f"note={nid}"),
                                                      (3, "update", f"note={nid}")])

    def test_更新は更新日時を変え_Noneは変えない_tagsの扱い(self):  # V9
        with mock.patch.object(service, "_now", return_value=100):
            nid = self.note(title="T", body="B", tags=["a", "b"])
        with mock.patch.object(service, "_now", return_value=200):
            service.update_note(self.conn, self.alice, nid, 1)  # 全 None でも版は上がる
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((got["title"], got["body"], got["tags"], got["version"], got["updated_at"]),
                         ("T", "B", ["a", "b"], 2, 200))
        service.update_note(self.conn, self.alice, nid, 2, tags=["c"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["c"])
        service.update_note(self.conn, self.alice, nid, 3, tags=())
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], [])

    def test_古いversionはConflictで変わらない(self):  # V9
        nid = self.note(tags=["a"])
        service.update_note(self.conn, self.alice, nid, 1, title="先")
        before = snapshot(self.conn)
        for ver in (1, 0, 3, -1, 2 ** 70):
            with self.subTest(version=ver), self.assertRaises(Conflict):
                service.update_note(self.conn, self.alice, nid, ver, title="後", tags=["z"])
        self.assertEqual(snapshot(self.conn), before)

    def test_更新の不正入力(self):  # V4
        nid = self.note()
        bad = [{"version": "1"}, {"version": True}, {"title": ""}, {"title": "x" * 201},
               {"body": "b" * 10001}, {"body": 1}, {"tags": "abc"}, {"tags": ["t" * 33]},
               {"tags": [str(i) for i in range(11)]}]
        before = snapshot(self.conn)
        for kw in bad:
            args = {"version": 1}
            args.update(kw)
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, nid, **args)
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.alice, "1", 1)
        self.assertEqual(snapshot(self.conn), before)

    def test_存在しないメモの更新と削除はNotFound(self):  # V8 V10
        for nid in (999, 2 ** 70):
            with self.subTest(nid=nid):
                with self.assertRaises(NotFound):
                    service.update_note(self.conn, self.alice, nid, 1)
                with self.assertRaises(NotFound):
                    service.delete_note(self.conn, self.alice, nid)

    def test_削除の可否と監査ログ(self):  # V8 V26
        n1 = self.note(self.alice, tags=["a"])
        n2 = self.note(self.alice, tags=["b"])
        before = snapshot(self.conn)
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, n1)
        self.assertEqual(snapshot(self.conn), before)  # 何も消えない
        service.delete_note(self.conn, self.alice, n1)
        service.delete_note(self.conn, self.root1, n2)
        self.assertEqual(counts(self.conn)["notes"], 0)
        self.assertEqual(counts(self.conn)["note_tags"], 0)
        self.assertEqual(audit_rows(self.conn)[2:], [(1, "delete", f"note={n1}"),
                                                      (3, "delete", f"note={n2}")])

    def test_二重削除はNotFound(self):  # V10
        nid = self.note()
        service.delete_note(self.conn, self.alice, nid)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.alice, nid)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.alice, nid)

    def test_delete_note_で失敗するとメモもタグも残る(self):  # V10 V26
        nid = self.note(tags=["a", "b"])
        seen = {}

        def boom(step):
            if step == "delete:note":
                # フックの時点ではタグは消えている（契約の呼び出し時点）
                seen["tags"] = counts(self.conn)["note_tags"]
                raise RuntimeError("途中失敗")
        before = snapshot(self.conn)
        with hook(boom), self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(seen["tags"], 0)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["a", "b"])

    def test_削除後に孤児タグが無い(self):  # V10
        nid = self.note(tags=["a", "b"])
        service.delete_note(self.conn, self.alice, nid)
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM note_tags t LEFT JOIN notes n ON n.id = t.note_id "
            "WHERE n.id IS NULL").fetchone()[0]
        self.assertEqual(orphans, 0)
