"""V21: 同時閲覧でも views が失われない（別接続で割り込みを再現）。"""
import pathlib
import tempfile

from notes import service
from tests.helpers import NotesTestCase


class ViewsConcurrencyTest(NotesTestCase):
    def test_views_フック中の別接続の閲覧が失われない(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(pathlib.Path(tmp.name) / "notes.db")
        a, b = self.open_db(path), self.open_db(path)
        self.add_users(a)
        nid = service.create_note(a, self.alice, "t", "b")
        seen: list[int] = []

        def hook(step: str) -> None:
            if step == "view" and not seen:
                seen.append(0)  # 再入を防ぐ
                # 確認後・増分前に、別接続で 2 回の閲覧が完了する
                seen[:] = [service.get_note(b, self.bob, nid)["views"], service.get_note(b, self.bob, nid)["views"]]

        self.set_hook(hook)
        got = service.get_note(a, self.alice, nid)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(got["views"], 3)  # 割り込みの 2 回と自分の 1 回の合計
        self.assertEqual(a.execute("SELECT views FROM notes WHERE id = ?", (nid,)).fetchone()[0], 3)
