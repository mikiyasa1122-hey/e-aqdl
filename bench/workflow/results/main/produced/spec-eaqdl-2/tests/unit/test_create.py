"""V1, V2: 作成の境界値・不正入力・create:tags 失敗。"""
from typing import ClassVar

from notes import service
from notes.errors import ValidationError
from tests.helpers import BaseCase


class CreateBoundaryTest(BaseCase):
    def test_題名の境界(self) -> None:
        for title in ("a", "あ" * 100):
            with self.subTest(len=len(title)):
                nid = self.make(self.alice, title=title)
                self.assertEqual(service.get_note(self.conn, self.alice, nid)["title"], title)

    def test_本文の境界(self) -> None:
        self.make(self.alice, body="")
        self.make(self.alice, body="x" * 10000)

    def test_タグの境界と重複は1件保存(self) -> None:
        nid = self.make(self.alice, tags=["a", "b" * 20, "c-1", "x", "a"][:5])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["a", "b" * 20, "c-1", "x"])
        nid2 = self.make(self.alice, tags=["dup", "dup"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid2)["tags"], ["dup"])

    def test_作成者と組織と非公開と監査(self) -> None:
        nid = self.make(self.alice, private=True)
        row = service.get_note(self.conn, self.alice, nid)
        self.assertEqual((row["org_id"], row["owner_id"], row["private"], row["version"]), (1, 2, 1, 1))
        a = self.conn.execute("SELECT * FROM audit").fetchall()
        self.assertEqual([(r["org_id"], r["actor_id"], r["action"], r["detail"]) for r in a],
                         [(1, 2, "create", f"note={nid}")])

    def test_create_tags失敗で何も残らない(self) -> None:
        self.fail_at("create:tags")
        before = self.counts()
        with self.assertRaises(RuntimeError):
            self.make(self.alice, tags=["a", "b"])
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.conn.in_transaction)


class CreateInvalidTest(BaseCase):
    """V2: 不正入力は ValidationError で、行数は変わらない。"""

    CASES: ClassVar[list] = [
        ("題名が空", {"title": ""}),
        ("題名が101文字", {"title": "a" * 101}),
        ("題名が空白のみ", {"title": "  　 "}),
        ("題名が None", {"title": None}),
        ("題名が数値", {"title": 5}),
        ("本文が10001文字", {"body": "x" * 10001}),
        ("本文が None", {"body": None}),
        ("タグ6個", {"tags": list("abcdef")}),
        ("重複を数えて6個", {"tags": ["a", "a", "a", "a", "a", "a"]}),
        ("タグが空文字", {"tags": [""]}),
        ("タグ21文字", {"tags": ["a" * 21]}),
        ("タグに大文字", {"tags": ["Abc"]}),
        ("タグに不正文字", {"tags": ["a_b"]}),
        ("タグに空白", {"tags": ["a b"]}),
        ("タグが文字列単体", {"tags": "abc"}),
        ("タグが set", {"tags": {"a"}}),
        ("タグが None", {"tags": None}),
        ("タグ要素が数値", {"tags": [1]}),
        ("タグに全角英数", {"tags": ["ａ"]}),
    ]

    def test_不正入力(self) -> None:
        for name, over in self.CASES:
            with self.subTest(name):
                before = self.counts()
                args = {"title": "題名", "body": "本文", "tags": ()}
                args.update(over)
                with self.assertRaises(ValidationError):
                    service.create_note(self.conn, self.alice, **args)  # type: ignore[arg-type]
                self.assertEqual(self.counts(), before)
                self.assertFalse(self.conn.in_transaction)
