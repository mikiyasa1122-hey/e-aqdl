"""service の単体テスト（V5 V6 V8 V9 V10 V16 V22 V23）。"""
import logging
import unittest

from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError
from tests.helpers import KEYS, make_env, snapshot


class CreateGetTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()

    def test_V5_create_get_全列とタグ(self):
        nid = service.create_note(self.conn, self.u[2], "題名", "本文", tags=["b", "a", "b", " a "], private=True)
        self.assertIsInstance(nid, int)
        got = service.get_note(self.conn, self.u[2], nid)
        self.assertEqual(set(got), {"id", "org_id", "owner_id", "title", "body", "private", "version",
                                    "views", "updated_at", "tags"})
        self.assertEqual((got["id"], got["org_id"], got["owner_id"], got["title"], got["body"]),
                         (nid, 1, 2, "題名", "本文"))
        self.assertEqual((got["private"], got["version"], got["views"]), (1, 1, 1))
        self.assertIsInstance(got["updated_at"], int)
        self.assertEqual(got["tags"], ["a", "b"])  # 重複なし・名前順
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 2)

    def test_V5_タグ無しは空リスト_公開が既定(self):
        nid = service.create_note(self.conn, self.u[2], "t", "")
        got = service.get_note(self.conn, self.u[2], nid)
        self.assertEqual((got["tags"], got["private"], got["body"]), ([], 0, ""))

    def test_V5_タグは大文字小文字を区別(self):
        nid = service.create_note(self.conn, self.u[2], "t", "b", tags=["A", "a"])
        self.assertEqual(service.get_note(self.conn, self.u[2], nid)["tags"], ["A", "a"])

    def test_V5_題名は前後の空白を除く(self):
        nid = service.create_note(self.conn, self.u[2], "  題名  ", "b")
        self.assertEqual(service.get_note(self.conn, self.u[2], nid)["title"], "題名")

    def test_V6_不正入力は何も残さない(self):
        cases = [
            ("title空", dict(title="")), ("title空白", dict(title="   ")),
            ("title201", dict(title="x" * 201)), ("title None", dict(title=None)),
            ("title int", dict(title=1)), ("title bytes", dict(title=b"abc")),
            ("body100001", dict(body="x" * 100001)), ("body None", dict(body=None)),
            ("body int", dict(body=5)),
            ("tags21", dict(tags=["t%d" % i for i in range(21)])),
            ("tag空", dict(tags=[""])), ("tag空白", dict(tags=["  "])),
            ("tag33", dict(tags=["x" * 33])), ("tag int", dict(tags=[1])),
            ("tag None", dict(tags=[None])), ("tags str", dict(tags="abc")),
            ("tags None", dict(tags=None)), ("tags int", dict(tags=5)),
        ]
        before = snapshot(self.conn)
        for name, override in cases:
            kwargs = dict(title="t", body="b", tags=[])
            kwargs.update(override)
            with self.subTest(name):
                with self.assertRaises(ValidationError):
                    service.create_note(self.conn, self.u[2], kwargs["title"], kwargs["body"], tags=kwargs["tags"])
                self.assertEqual(snapshot(self.conn), before)
                self.assertFalse(self.conn.in_transaction)

    def test_V6_境界ちょうどは成功(self):
        tags = ["%032d" % i for i in range(20)]
        nid = service.create_note(self.conn, self.u[2], "x" * 200, "y" * 100000, tags=tags)
        got = service.get_note(self.conn, self.u[2], nid)
        self.assertEqual((len(got["title"]), len(got["body"]), len(got["tags"])), (200, 100000, 20))
        service.create_note(self.conn, self.u[2], "x", "", tags=["x" * 32])

    def test_V6_update側の不正入力も変更を残さない(self):
        nid = service.create_note(self.conn, self.u[2], "t", "b", tags=["a"])
        before = snapshot(self.conn)
        for kwargs in (dict(title=""), dict(title="x" * 201), dict(body="x" * 100001), dict(tags=["x" * 33]),
                       dict(tags="abc"), dict(title=5)):
            with self.subTest(kwargs):
                with self.assertRaises(ValidationError):
                    service.update_note(self.conn, self.u[2], nid, 1, **kwargs)
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.u[2], nid, "1", title="x")
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.u[2], nid, True, title="x")
        self.assertEqual(snapshot(self.conn), before)


class ListTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()

    def _make(self, titles, updated):
        ids = []
        for title in titles:
            ids.append(service.create_note(self.conn, self.u[2], title, "b"))
        for nid, ts in zip(ids, updated):
            self.conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (ts, nid))
        return ids

    def test_V8_updated降順_同値はid降順(self):
        ids = self._make(["a", "b", "c", "d"], [100, 200, 200, 50])
        res = service.list_notes(self.conn, self.u[2])
        self.assertEqual([i["id"] for i in res["items"]], [ids[2], ids[1], ids[0], ids[3]])
        self.assertEqual(res["total"], 4)
        self.assertEqual(set(res["items"][0]), {"id", "title", "updated_at"})
        self.assertEqual((res["page"], res["per_page"]), (1, 20))

    def test_V8_title昇順_同値はid昇順(self):
        ids = self._make(["b", "a", "b", "a"], [1, 1, 1, 1])
        res = service.list_notes(self.conn, self.u[2], sort="title")
        self.assertEqual([i["id"] for i in res["items"]], [ids[1], ids[3], ids[0], ids[2]])

    def test_V8_ページ境界と範囲外(self):
        ids = self._make(["t%d" % i for i in range(5)], [1, 2, 3, 4, 5])
        p1 = service.list_notes(self.conn, self.u[2], page=1, per_page=2)
        p3 = service.list_notes(self.conn, self.u[2], page=3, per_page=2)
        p4 = service.list_notes(self.conn, self.u[2], page=4, per_page=2)
        self.assertEqual([i["id"] for i in p1["items"]], [ids[4], ids[3]])
        self.assertEqual([i["id"] for i in p3["items"]], [ids[0]])
        self.assertEqual((p4["items"], p4["total"], p4["page"], p4["per_page"]), ([], 5, 4, 2))

    def test_V8_per_page境界(self):
        self._make(["a"], [1])
        self.assertEqual(len(service.list_notes(self.conn, self.u[2], per_page=1)["items"]), 1)
        self.assertEqual(service.list_notes(self.conn, self.u[2], per_page=100)["per_page"], 100)

    def test_V8_不正引数(self):
        for kwargs in (dict(page=0), dict(page=-1), dict(page="1"), dict(page=True), dict(page=1.0),
                       dict(per_page=0), dict(per_page=101), dict(per_page="5"), dict(per_page=True),
                       dict(sort="id"), dict(sort=None), dict(sort="title; DROP TABLE notes")):
            with self.subTest(kwargs):
                with self.assertRaises(ValidationError):
                    service.list_notes(self.conn, self.u[2], **kwargs)
        self.assertFalse(self.conn.in_transaction)

    def test_V8_見えるメモだけを数える(self):
        service.create_note(self.conn, self.u[2], "pub", "b")
        service.create_note(self.conn, self.u[2], "priv", "b", private=True)
        service.create_note(self.conn, self.u[12], "other-org", "b")
        self.assertEqual(service.list_notes(self.conn, self.u[2])["total"], 2)
        self.assertEqual(service.list_notes(self.conn, self.u[3])["total"], 1)
        self.assertEqual(service.list_notes(self.conn, self.u[1])["total"], 1)


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()

    def _ids(self, q, tags=(), user=None):
        return [i["id"] for i in service.search_notes(self.conn, user or self.u[2], q, tags=tags)]

    def test_V9_部分一致_題名と本文_大文字小文字(self):
        a = service.create_note(self.conn, self.u[2], "Hello World", "x")
        b = service.create_note(self.conn, self.u[2], "x", "say HELLO there")
        service.create_note(self.conn, self.u[2], "other", "none")
        self.assertEqual(self._ids("hello"), [a, b])
        self.assertEqual(self._ids("LO WOR"), [a])
        res = service.search_notes(self.conn, self.u[2], "hello")
        self.assertEqual(set(res[0]), {"id", "title"})

    def test_V9_パーセントとアンダースコアはリテラル(self):
        a = service.create_note(self.conn, self.u[2], "100% sure", "x")
        b = service.create_note(self.conn, self.u[2], "a_b", "x")
        service.create_note(self.conn, self.u[2], "100 sure", "x")
        service.create_note(self.conn, self.u[2], "axb", "x")
        c = service.create_note(self.conn, self.u[2], "back\\slash", "x")
        self.assertEqual(self._ids("%"), [a])
        self.assertEqual(self._ids("_"), [b])
        self.assertEqual(self._ids("a_b"), [b])
        self.assertEqual(self._ids("\\"), [c])
        self.assertEqual(self._ids("k\\s"), [c])

    def test_V9_タグANDで絞り込む(self):
        a = service.create_note(self.conn, self.u[2], "memo", "x", tags=["x", "y"])
        service.create_note(self.conn, self.u[2], "memo", "x", tags=["x"])
        service.create_note(self.conn, self.u[2], "memo", "x", tags=["y", "z"])
        self.assertEqual(self._ids("memo", tags=["x", "y"]), [a])
        self.assertEqual(len(self._ids("memo", tags=["x"])), 2)
        self.assertEqual(self._ids("memo", tags=["x", "x", "y"]), [a])  # 重複は1つ扱い
        self.assertEqual(self._ids("memo", tags=["nope"]), [])
        self.assertEqual(len(self._ids("memo")), 3)

    def test_V9_空qと不正はValidationError(self):
        for q in ("", "   ", None, 5, b"x"):
            with self.subTest(q=q):
                with self.assertRaises(ValidationError):
                    service.search_notes(self.conn, self.u[2], q)
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.u[2], "x", tags="abc")
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.u[2], "x", tags=[""])

    def test_V9_検索語の空白は削らない(self):
        a = service.create_note(self.conn, self.u[2], "foo bar", "x")
        service.create_note(self.conn, self.u[2], "foobar", "x")
        self.assertEqual(self._ids("o b"), [a])


class UpdateDeleteTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()
        self.nid = service.create_note(self.conn, self.u[2], "t", "b", tags=["a", "b"])

    def _row(self):
        return dict(self.conn.execute("SELECT * FROM notes WHERE id = ?", (self.nid,)).fetchone())

    def test_V10_作成者と管理者は更新でき他memberはForbidden(self):
        self.assertEqual(service.update_note(self.conn, self.u[2], self.nid, 1, title="x1"), 2)
        self.assertEqual(service.update_note(self.conn, self.u[1], self.nid, 2, title="x2"), 3)
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.u[3], self.nid, 3, title="x3")
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.u[3], self.nid)
        self.assertEqual((self._row()["title"], self._row()["version"]), ("x2", 3))

    def test_V10_None項目は不変_tagsは置換_updated_at更新(self):
        self.conn.execute("UPDATE notes SET updated_at = 5 WHERE id = ?", (self.nid,))
        v = service.update_note(self.conn, self.u[2], self.nid, 1, body="new")
        row = self._row()
        self.assertEqual((v, row["title"], row["body"], row["version"]), (2, "t", "new", 2))
        self.assertGreater(row["updated_at"], 5)
        self.assertEqual(service.get_note(self.conn, self.u[2], self.nid)["tags"], ["a", "b"])  # tags None は不変
        service.update_note(self.conn, self.u[2], self.nid, 2, tags=["z"])
        self.assertEqual(service.get_note(self.conn, self.u[2], self.nid)["tags"], ["z"])
        service.update_note(self.conn, self.u[2], self.nid, 3, tags=[])
        self.assertEqual(service.get_note(self.conn, self.u[2], self.nid)["tags"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)

    def test_V10_古いversionはConflictで何も変わらない(self):
        service.update_note(self.conn, self.u[2], self.nid, 1, title="x")
        before = snapshot(self.conn)
        for ver in (1, 3, 0):
            with self.assertRaises(Conflict):
                service.update_note(self.conn, self.u[2], self.nid, ver, title="y", tags=["q"])
        self.assertEqual(snapshot(self.conn), before)
        self.assertFalse(self.conn.in_transaction)

    def test_V10_削除でタグも消える_存在しないidはNotFound(self):
        service.delete_note(self.conn, self.u[1], self.nid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)
        for func in (lambda: service.delete_note(self.conn, self.u[1], self.nid),
                     lambda: service.update_note(self.conn, self.u[1], 9999, 1, title="x")):
            with self.assertRaises(NotFound):
                func()


class SecurityUnitTest(unittest.TestCase):
    def setUp(self):
        self.conn, self.u = make_env()

    def test_V16_伏せ字(self):
        for key, expected in [("", ""), ("a", "*"), ("abcd", "****"), ("abcde", "*bcde"),
                              ("0123456789", "******6789")]:
            self.assertEqual(service._mask_key(key), expected)
            self.assertEqual(len(service._mask_key(key)), len(key))

    def test_V16_export_audit_に_api_key_全文が出ない(self):
        service.create_note(self.conn, self.u[2], "t", "b")
        service.create_note(self.conn, self.u[1], "t2", "b")
        rows = service.export_audit(self.conn, self.u[1])
        text = repr(rows)
        for key in KEYS.values():
            self.assertNotIn(key, text)
        self.assertEqual(rows[0]["actor_key"], "*" * (len(KEYS[2]) - 4) + KEYS[2][-4:])
        self.assertEqual(rows[0]["actor"], "a2")
        self.assertEqual(set(rows[0]), {"id", "action", "detail", "actor", "actor_key"})

    def test_V22_SQL断片や記号がそのまま保存・検索される(self):
        title = "x'); DROP TABLE notes;-- \"q\" 100% a_b 日本語🙂"
        tag = "t'; DELETE FROM users;--"
        nid = service.create_note(self.conn, self.u[2], title, "body %_ ' \"", tags=[tag])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)
        got = service.get_note(self.conn, self.u[2], nid)
        self.assertEqual((got["title"], got["tags"]), (title, [tag]))
        self.assertEqual(service.search_notes(self.conn, self.u[2], "'); DROP TABLE notes;--"),
                         [{"id": nid, "title": title}])
        self.assertEqual(service.search_notes(self.conn, self.u[2], "日本語", tags=[tag]), [{"id": nid, "title": title}])
        self.assertEqual(service.search_notes(self.conn, self.u[2], "' OR '1'='1"), [])
        self.assertEqual(service.search_notes(self.conn, self.u[12], "DROP"), [])  # 他組織には出ない
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 5)

    def test_V23_ログにidだけが出て本文などは出ない(self):
        with self.assertLogs("notes", "INFO") as cm:
            nid = service.create_note(self.conn, self.u[2], "TITLE-SECRET-XYZ", "BODY-SECRET-XYZ", tags=["TAG-XYZ"])
            service.get_note(self.conn, self.u[2], nid)
            service.update_note(self.conn, self.u[2], nid, 1, title="TITLE2-SECRET")
            service.export_audit(self.conn, self.u[1])
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("id=%d" % nid, text)
        self.assertIn("owner=2", text)
        for secret in ("TITLE", "BODY", "TAG-XYZ", *KEYS.values()):
            self.assertNotIn(secret, text)
        self.assertTrue(all(r.levelno == logging.INFO for r in cm.records))

    def test_V23_audit_detailに題名や本文が入らない(self):
        nid = service.create_note(self.conn, self.u[2], "TITLE-SECRET-XYZ", "BODY-SECRET-XYZ", private=True)
        service.update_note(self.conn, self.u[2], nid, 1, title="TITLE2")
        service.delete_note(self.conn, self.u[2], nid)
        details = [r["detail"] for r in service.export_audit(self.conn, self.u[1])]
        self.assertEqual(details, ["note=%d" % nid] * 3)


if __name__ == "__main__":
    unittest.main()
