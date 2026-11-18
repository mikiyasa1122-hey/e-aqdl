"""AC3 AC4 AC5 AC18 AC19（一部）: 作成・取得・入力検証。"""
from unittest import mock

from notes import service
from notes.errors import NotFound, ValidationError
from tests.helpers import MemCase, counts, hook


class CreateGetUnit(MemCase):
    def test_作成と取得で全列と名前順のtagsとviews加算(self):  # V3
        with mock.patch.object(service, "_now", return_value=1700000000):
            nid = self.note(title="  題名  ", body="本文", tags=["zeta", "alpha", "mid", "alpha"],
                            private=True)
        self.assertIsInstance(nid, int)
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(got, {"id": nid, "org_id": 1, "owner_id": 1, "title": "題名",
                               "body": "本文", "private": 1, "version": 1, "views": 1,
                               "updated_at": 1700000000, "tags": ["alpha", "mid", "zeta"]})
        again = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(again["views"], 2)
        self.assertEqual(again["updated_at"], 1700000000)  # 閲覧では更新日時を変えない

    def test_作成の監査ログ(self):  # V3
        nid = self.note()
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT org_id, actor_id, action, detail FROM audit")]
        self.assertEqual(rows, [(1, 1, "create", f"note={nid}")])

    def test_存在しないメモはNotFound(self):  # V3
        for bad in (999, 0, -1, 2 ** 70):
            with self.subTest(bad=bad), self.assertRaises(NotFound):
                service.get_note(self.conn, self.alice, bad)

    def test_get_note_の型違いはValidationError(self):  # V23
        for bad in ("1", None, 1.0, True, [1]):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                service.get_note(self.conn, self.alice, bad)

    def test_境界値の前後(self):  # V4
        ok = [
            ("title200字", {"title": "あ" * 200}),
            ("title前後の空白は除く", {"title": " " * 5 + "x" * 200 + " " * 5}),
            ("body10000字", {"body": "b" * 10000}),
            ("body空", {"body": ""}),
            ("tag32字", {"tags": ["t" * 32]}),
            ("tag10個", {"tags": [f"t{i}" for i in range(10)]}),
            ("tag重複は除いて10個", {"tags": [f"t{i}" for i in range(10)] * 3}),
            ("tag空集合", {"tags": ()}),
            ("tagsはset", {"tags": {"a", "b"}}),
        ]
        for name, kw in ok:
            with self.subTest(name):
                self.assertIsInstance(self.note(**kw), int)
        bad = [
            ("title空", {"title": ""}),
            ("title空白のみ", {"title": "   \t\n"}),
            ("title201字", {"title": "あ" * 201}),
            ("title非文字列", {"title": None}),
            ("body10001字", {"body": "b" * 10001}),
            ("body非文字列", {"body": 123}),
            ("body None", {"body": None}),
            ("bodyがbytes", {"body": b"x"}),
            ("tag空文字", {"tags": [""]}),
            ("tag空白のみ", {"tags": ["  "]}),
            ("tag33字", {"tags": ["t" * 33]}),
            ("tag11個", {"tags": [f"t{i}" for i in range(11)]}),
            ("tag非文字列", {"tags": [1]}),
            ("tags裸の文字列", {"tags": "abc"}),
            ("tags None", {"tags": None}),
            ("tags dict", {"tags": {"a": 1}}),
            ("tags bytes", {"tags": b"ab"}),
            ("private非bool", {"private": 1}),
            ("private None", {"private": None}),
            ("title孤立サロゲート", {"title": "a\ud800"}),
            ("body孤立サロゲート", {"body": "\udc00"}),
            ("tag孤立サロゲート", {"tags": ["\ud800"]}),
        ]
        before = counts(self.conn)
        for name, kw in bad:
            with self.subTest(name), self.assertRaises(ValidationError):
                self.note(**kw)
        self.assertEqual(counts(self.conn), before)  # 失敗では何も残らない

    def test_不正入力で行が残らない_ファズ的(self):  # V23
        big = "x" * 100000
        invalid = {
            "title": [None, 0, 2 ** 64, 1.5, b"t", object(), [], {}, big, "", "\ud800" * 10],
            "body": [None, 0, 2 ** 64, float("nan"), b"b", object(), [], {}, big, "\udc00"],
            "tags": [None, 0, "abc", b"abc", object(), {}, ["a"] * 1 + [None], [1],
                     ["y" * 100000], [f"t{i}" for i in range(1000)], [big], ["\ud800"], [""]],
            "private": [None, 0, 1, "x", [], object(), 1.5],
        }
        before = counts(self.conn)
        for field, values in invalid.items():
            for i, v in enumerate(values):
                kw = {"title": "t", "body": "b", "tags": (), "private": False}
                kw[field] = v
                with self.subTest(field=field, i=i), self.assertRaises(ValidationError):
                    service.create_note(self.conn, self.alice, **kw)
        self.assertEqual(counts(self.conn), before)

    def test_create_tags_で失敗すると何も残らない(self):  # V5
        def boom(step):
            if step == "create:tags":
                raise RuntimeError("途中失敗")
        with hook(boom), self.assertRaises(RuntimeError):
            self.note(tags=["a", "b"])
        self.assertNoRows()

    def test_create_tags_は挿入後でタグ挿入前に呼ばれる(self):  # V5
        seen = {}

        def spy(step):
            seen["step"] = step
            seen["notes"] = counts(self.conn)["notes"]
            seen["tags"] = counts(self.conn)["note_tags"]
        with hook(spy):
            self.note(tags=["a"])
        self.assertEqual(seen, {"step": "create:tags", "notes": 1, "tags": 0})

    def test_存在しない利用者はValidationErrorで何も残らない(self):  # V5
        ghost = {"id": 999, "org_id": 1, "name": "x", "role": "member", "api_key": "k"}
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, ghost, "t", "b")
        self.assertNoRows()

    def test_SQL注入文字列は値として保存される(self):  # V18
        evil = "x'); DROP TABLE notes; --"
        nid = self.note(title=evil, body=evil, tags=[evil[:32]])
        got = service.get_note(self.conn, self.alice, nid)
        self.assertEqual(got["title"], evil)
        self.assertEqual(got["body"], evil)
        self.assertEqual(got["tags"], [evil[:32]])
        self.assertEqual(counts(self.conn)["notes"], 1)
