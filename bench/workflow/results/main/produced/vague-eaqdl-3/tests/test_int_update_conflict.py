"""V10 V11: 楽観的排他（ファイル DB・別接続で割り込みを再現）。"""
import pathlib
import tempfile

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound
from tests.helpers import NotesTestCase


class UpdateConflictTest(NotesTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(pathlib.Path(tmp.name) / "notes.db")
        self.a = self.open_db(self.path)  # 遅れて書く側
        self.b = self.open_db(self.path)  # 割り込む側
        self.add_users(self.a)
        self.nid = service.create_note(self.a, self.alice, "元の題名", "元の本文", tags=["orig"])

    def _row(self) -> dict:
        got = dict(self.a.execute("SELECT * FROM notes WHERE id = ?", (self.nid,)).fetchone())
        got["tags"] = [r[0] for r in self.a.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag",
                                                    (self.nid,))]
        return got

    def test_conflict_古いversionはConflictでメモは変化しない(self) -> None:
        self.assertEqual(service.update_note(self.a, self.alice, self.nid, 1, title="v2"), 2)
        before = self.snapshot(self.a)
        for stale in (1, 3, 0):
            with self.assertRaises(Conflict):
                service.update_note(self.a, self.alice, self.nid, stale, title="stale", tags=["x"])
        self.assertEqual(self.snapshot(self.a), before)

    def test_conflict_フック中に他者が更新すると遅れた側がConflictで他者の変更が残る(self) -> None:
        fired: list[int] = []

        def hook(step: str) -> None:
            if step == "update" and not fired:
                fired.append(1)
                # 別接続で、同じ version を持つ別の利用者（管理者）が先に更新を成功させる
                self.assertEqual(service.update_note(self.b, self.carol, self.nid, 1, title="他者の題名",
                                                     tags=["other"]), 2)

        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.a, self.alice, self.nid, 1, title="遅れた側", body="遅れた本文",
                                tags=["late"])
        self.assertEqual(fired, [1])
        row = self._row()
        self.assertEqual((row["title"], row["body"], row["version"], row["tags"]),
                         ("他者の題名", "元の本文", 2, ["other"]))
        actions = [r["detail"] for r in self.a.execute("SELECT * FROM audit WHERE action='update'")]
        self.assertEqual(actions, [f"note={self.nid}"])  # 失敗した側は監査に残らない

    def test_conflict_version条件のWHEREが最終防衛線(self) -> None:
        # 早期の version 確認は通過済みで、トランザクション内の再確認でも version 不一致を拾えるのは
        # UPDATE の WHERE version=? だけ。フック中に別接続が成功させた更新が上書きされないこと
        fired: list[int] = []

        def hook(step: str) -> None:
            if step == "update" and not fired:
                fired.append(1)
                service.update_note(self.b, self.alice, self.nid, 1, body="他者の本文")

        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.a, self.alice, self.nid, 1, title="遅れた題名")
        row = self._row()
        self.assertEqual((row["title"], row["body"], row["version"]), ("元の題名", "他者の本文", 2))
        self.assertFalse(self.a.in_transaction)

    def test_conflict_フック中の割り込みでtagsも上書きしない(self) -> None:
        fired: list[int] = []

        def hook(step: str) -> None:
            if step == "update" and not fired:
                fired.append(1)
                service.update_note(self.b, self.alice, self.nid, 1, tags=["mine"])

        self.set_hook(hook)
        with self.assertRaises(Conflict):
            service.update_note(self.a, self.alice, self.nid, 1, tags=["late"])
        self.assertEqual(self._row()["tags"], ["mine"])

    def test_conflict_フック中の削除はNotFound(self) -> None:
        fired: list[int] = []

        def hook(step: str) -> None:
            if step == "update" and not fired:
                fired.append(1)
                service.delete_note(self.b, self.alice, self.nid)

        self.set_hook(hook)
        with self.assertRaises(NotFound):
            service.update_note(self.a, self.alice, self.nid, 1, title="x")

    def test_conflict_フック中に権限を失ったらForbidden(self) -> None:
        # フック中に引き継がれ、alice は作成者でも管理者でもなくなる
        fired: list[int] = []

        def hook(step: str) -> None:
            if step == "update" and not fired:
                fired.append(1)
                service.transfer_notes(self.b, self.carol, self.alice, self.bob)

        self.set_hook(hook)
        with self.assertRaises(Forbidden):
            service.update_note(self.a, self.alice, self.nid, 1, title="x")
        self.assertEqual(self._row()["title"], "元の題名")
        self.assertEqual(db.get_user(self.a, 1)["id"], 1)
