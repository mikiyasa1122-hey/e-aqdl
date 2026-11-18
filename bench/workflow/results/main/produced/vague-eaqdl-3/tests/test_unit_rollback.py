"""V24: 途中失敗のロールバック。"""
from notes import service
from tests.helpers import NotesTestCase


class Boom(Exception):
    pass


class RollbackTest(NotesTestCase):
    def _raise_on(self, target: str) -> None:
        def hook(step: str) -> None:
            if step == target:
                raise Boom(step)
        self.set_hook(hook)

    def test_rollback_create_tagsで失敗するとメモも監査も残らない(self) -> None:
        service.create_note(self.conn, self.alice, "既存", "b", tags=["x"])
        before = self.snapshot()
        self._raise_on("create:tags")
        with self.assertRaises(Boom):
            service.create_note(self.conn, self.alice, "新規", "b", tags=["a", "b"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_rollback_create失敗時はログも出ない(self) -> None:
        self._raise_on("create:tags")
        with self.assertNoLogs("notes", level="DEBUG"), self.assertRaises(Boom):
            service.create_note(self.conn, self.alice, "新規", "b")

    def test_rollback_delete_noteで失敗するとタグも残る(self) -> None:
        nid = service.create_note(self.conn, self.alice, "t", "b", tags=["x", "y"])
        before = self.snapshot()
        self._raise_on("delete:note")
        with self.assertRaises(Boom):
            service.delete_note(self.conn, self.alice, nid)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.conn.in_transaction)
        # タグが残っていることを直接確認する
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["x", "y"])

    def test_rollback_失敗後も同じ接続で操作を続けられる(self) -> None:
        self._raise_on("create:tags")
        with self.assertRaises(Boom):
            service.create_note(self.conn, self.alice, "新規", "b")
        service.hooks.__dict__.pop("before_write")
        nid = service.create_note(self.conn, self.alice, "新規", "b", tags=["a"])
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["a"])
