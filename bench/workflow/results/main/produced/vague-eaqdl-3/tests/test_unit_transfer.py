"""V12 V13: 引き継ぎ。"""
from notes import db, service
from notes.errors import Forbidden, NotFound, ValidationError
from tests.helpers import NotesTestCase


class TransferTest(NotesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.n1 = service.create_note(self.conn, self.alice, "1", "b")
        self.n2 = service.create_note(self.conn, self.alice, "2", "b", private=True)
        self.n3 = service.create_note(self.conn, self.alice, "3", "b")
        self.bobs = service.create_note(self.conn, self.bob, "bob", "b")
        self.dave_note = service.create_note(self.conn, self.dave, "d", "b")

    def _owner_version(self) -> dict[int, tuple]:
        return {r["id"]: (r["owner_id"], r["version"]) for r in self.conn.execute("SELECT * FROM notes")}

    def test_transfer_件数とversionと監査(self) -> None:
        count = service.transfer_notes(self.conn, self.carol, self.alice, self.bob)
        self.assertEqual(count, 3)
        got = self._owner_version()
        for nid in (self.n1, self.n2, self.n3):
            self.assertEqual(got[nid], (self.bob["id"], 2))
        self.assertEqual(got[self.bobs], (self.bob["id"], 1))
        self.assertEqual(got[self.dave_note], (self.dave["id"], 1))
        last = [dict(r) for r in self.conn.execute("SELECT * FROM audit ORDER BY id")][-1]
        self.assertEqual((last["action"], last["detail"], last["actor_id"], last["org_id"]),
                         ("transfer", "from=1 to=2 count=3", 3, 1))

    def test_transfer_idでも指定でき0件でも成功し監査が残る(self) -> None:
        self.assertEqual(service.transfer_notes(self.conn, self.carol, self.alice["id"], self.bob["id"]), 3)
        self.assertEqual(service.transfer_notes(self.conn, self.carol, self.alice, self.bob), 0)
        last = self.conn.execute("SELECT detail FROM audit ORDER BY id DESC").fetchone()[0]
        self.assertEqual(last, "from=1 to=2 count=0")

    def test_transfer_非管理者はForbidden(self) -> None:
        before = self.snapshot()
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.alice, self.alice, self.bob)
        self.assertEqual(self.snapshot(), before)

    def test_transfer_他組織の対象と他組織管理者はForbidden(self) -> None:
        before = self.snapshot()
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.carol, self.dave, self.bob)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.carol, self.alice, self.dave)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.erin, self.alice, self.bob)
        self.assertEqual(self.snapshot(), before)

    def test_transfer_呼び出し側dictのorgを偽っても越境できない(self) -> None:
        before = self.snapshot()
        fake_dave = dict(self.dave, org_id=1)
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.carol, self.alice, fake_dave)
        # id だけを渡しても DB の org で判定される
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.carol, self.dave["id"], self.alice)
        self.assertEqual(self.snapshot(), before)

    def test_transfer_不正な対象(self) -> None:
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.carol, self.alice, self.alice)
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.carol, self.alice, "2")
        with self.assertRaises(ValidationError):
            service.transfer_notes(self.conn, self.carol, self.alice, None)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.carol, self.alice, 999)
        with self.assertRaises(NotFound):
            service.transfer_notes(self.conn, self.carol, 10**30, self.alice)

    def test_transfer_途中で例外なら1件も移らない(self) -> None:
        # V13: 2 件目の移動直前で例外。1 件目は移動済みのはずなので、巻き戻りを確かめる
        before = self.snapshot()
        steps: list[str] = []

        def hook(step: str) -> None:
            steps.append(step)
            if step == f"transfer:{self.n2}":
                raise RuntimeError("途中失敗")

        self.set_hook(hook)
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.carol, self.alice, self.bob)
        self.assertEqual(steps, [f"transfer:{self.n1}", f"transfer:{self.n2}"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.conn.in_transaction)
        # 失敗後に再試行できる
        service.hooks.__dict__.pop("before_write")
        self.assertEqual(service.transfer_notes(self.conn, self.carol, self.alice, self.bob), 3)
        self.assertTrue(db.get_user(self.conn, 1))
