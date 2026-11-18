"""V22 V23: 入力検証とインジェクション・巨大入力。"""
from typing import Any

from notes import service
from notes.errors import Conflict, NotFound, ValidationError
from tests.helpers import NotesTestCase


class ValidationTest(NotesTestCase):
    def test_validation_createの不正入力表(self) -> None:
        bad_cases: list[dict[str, Any]] = [
            {"title": ""}, {"title": "   "}, {"title": "\t\n"}, {"title": None}, {"title": 5},
            {"title": b"x"}, {"title": "a" * 201}, {"title": "\ud800"},
            {"body": None}, {"body": 5}, {"body": b"x"}, {"body": "a" * 10001},
            {"tags": "abc"}, {"tags": [""]}, {"tags": ["  "]}, {"tags": [5]}, {"tags": [None]},
            {"tags": ["a" * 51]}, {"tags": [f"t{i}" for i in range(21)]}, {"tags": 5}, {"tags": None},
            {"private": 1}, {"private": "no"}, {"private": None},
        ]
        before = self.snapshot()
        for kwargs in bad_cases:
            args: dict[str, Any] = {"title": "題", "body": "本", "tags": (), "private": False}
            args.update(kwargs)
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                service.create_note(self.conn, self.alice, **args)
        self.assertEqual(self.snapshot(), before)

    def test_validation_境界値は通る(self) -> None:
        nid = service.create_note(self.conn, self.alice, "a" * 200, "b" * 10000,
                                  tags=[f"{i:02d}" + "x" * 48 for i in range(20)])
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((len(got["title"]), len(got["body"]), len(got["tags"])), (200, 10000, 20))
        # 重複を除けば 20 個まで。重複込みで 20 個を超えても通る
        service.create_note(self.conn, self.alice, "t", "", tags=["a"] * 30)
        service.create_note(self.conn, self.alice, "t", "", tags={"a", "b"})
        service.create_note(self.conn, self.alice, "t", "", tags=("a",))

    def test_validation_updateの不正入力(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b")
        before = self.snapshot()
        for kwargs in ({"version": "1"}, {"version": None}, {"version": True}, {"version": 1.0},
                       {"version": 1, "title": ""}, {"version": 1, "title": 5}, {"version": 1, "body": 5},
                       {"version": 1, "tags": "a"}, {"version": 1, "tags": [""]}, {"version": 1, "title": "a" * 201}):
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                service.update_note(self.conn, self.alice, nid, **kwargs)
        for bad_id in ("1", None, 1.5, True):
            with self.assertRaises(ValidationError):
                service.get_note(self.conn, self.alice, bad_id)
        self.assertEqual(self.snapshot(), before)

    def test_validation_巨大なidはNotFound(self) -> None:
        for fn in (lambda i: service.get_note(self.conn, self.alice, i),
                   lambda i: service.update_note(self.conn, self.alice, i, 1, title="x"),
                   lambda i: service.delete_note(self.conn, self.alice, i)):
            with self.assertRaises(NotFound):
                fn(10**30)
        nid = service.create_note(self.conn, self.alice, "t", "b")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 10**30, title="x")

    def test_validation_searchの不正入力(self) -> None:
        for q, tags in ((None, ()), (5, ()), ("a" * 10001, ()), ("x", "abc"), ("x", [""]), ("x", [5]),
                        ("x", [f"t{i}" for i in range(21)]), ("\ud800", ())):
            with self.assertRaises(ValidationError, msg=repr((q, tags))):
                service.search_notes(self.conn, self.alice, q, tags=tags)

    def test_injection_特殊文字の題名本文タグがそのまま保存される(self) -> None:
        nasty = ["'; DROP TABLE notes; --", "' OR '1'='1", "100%", "a_b", "back\\slash", '"quoted"',
                 "x'); DELETE FROM notes; --", "%' OR 1=1 --", "日本語　全角", "emoji \U0001f600", "a\x00b"]
        ids = []
        for s in nasty:
            ids.append(service.create_note(self.conn, self.alice, s, s, tags=[s]))
        for nid, s in zip(ids, nasty):
            got = service.get_note(self.conn, self.alice, nid)
            self.assertEqual((got["title"], got["body"], got["tags"]), (s, s, [s]))
            self.assertEqual([i["id"] for i in service.search_notes(self.conn, self.alice, "", tags=[s])], [nid])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], len(nasty))

    def test_injection_検索語が条件を広げない(self) -> None:
        service.create_note(self.conn, self.alice, "普通", "本文")
        service.create_note(self.conn, self.dave, "他組織", "本文")
        for q in ("' OR '1'='1", "%", "_", "%%", "\\", "' OR 1=1 --", "x') OR ('1'='1"):
            self.assertEqual(service.search_notes(self.conn, self.alice, q), [], msg=q)
        self.assertEqual(len(service.search_notes(self.conn, self.alice, "")), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2)

    def test_injection_タイトル更新でもSQLが壊れない(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b")
        service.update_note(self.conn, self.alice, nid, 1, title="x', version = 999 --", body="%_")
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((got["title"], got["body"], got["version"]), ("x', version = 999 --", "%_", 2))

    def test_validation_例外メッセージに入力値を含めない(self) -> None:
        secret = "SECRET-VALUE-" + "x" * 20
        for fn in (lambda: service.create_note(self.conn, self.alice, secret * 20, "b"),
                   lambda: service.create_note(self.conn, self.alice, "t", "b", tags=[secret * 5]),
                   lambda: service.search_notes(self.conn, self.alice, secret * 1000)):
            with self.assertRaises(ValidationError) as cm:
                fn()
            self.assertNotIn(secret, str(cm.exception))
