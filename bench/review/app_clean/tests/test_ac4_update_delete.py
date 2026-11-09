"""AC4・K1・K3・K4: R4 の規則どおりに更新・削除・競合・拒否が決まる。"""
from notes import service
from notes.errors import Conflict, Forbidden, NotFound, ValidationError

from .base import Base


class UpdateTest(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["a"])

    def note(self):
        return service.get_note(self.conn, self.alice, self.nid)

    def test_owner_and_same_org_admin_can_update(self):
        self.assertEqual(service.update_note(self.conn, self.alice, self.nid, 1, body="c"), 2)
        self.assertEqual(service.update_note(self.conn, self.admin, self.nid, 2, title="t2", tags=["x", "x"]), 3)
        n = self.note()
        self.assertEqual((n["title"], n["body"], n["tags"], n["version"]), ("t2", "c", ["x"], 3))

    def test_others_are_forbidden_or_not_found(self):
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, self.nid, 1, body="x")
        for user in (self.eve, self.eve_admin):
            with self.subTest(user=user["name"]), self.assertRaises(NotFound):
                service.update_note(self.conn, user, self.nid, 1, body="x")
        with self.assertRaises(NotFound):
            service.update_note(self.conn, self.alice, 999, 1, body="x")
        self.assertEqual(self.note()["version"], 1)

    def test_private_note_only_owner(self):
        priv = service.create_note(self.conn, self.alice, "p", "b", private=True)
        for user in (self.admin, self.bob):
            with self.subTest(user=user["name"]), self.assertRaises(NotFound):
                service.update_note(self.conn, user, priv, 1, body="x")
            with self.subTest(user=user["name"]), self.assertRaises(NotFound):
                service.delete_note(self.conn, user, priv)
        service.update_note(self.conn, self.alice, priv, 1, body="x")

    def test_stale_version_conflicts(self):
        service.update_note(self.conn, self.alice, self.nid, 1, body="c")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, body="d")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 99, body="d")
        self.assertEqual(self.note()["body"], "c")

    def test_concurrent_update_between_read_and_write_conflicts(self):
        done = []

        def other(step):
            if step == "update" and not done:
                done.append(1)
                service.update_note(self.conn, self.admin, self.nid, 1, body="other")
        service.hooks.before_write = other
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 1, title="mine")
        n = self.note()
        self.assertEqual((n["title"], n["body"], n["version"]), ("t", "other", 2))

    def test_unchanged_fields_come_from_the_latest_row(self):
        """読み出しの後に他の更新が入り、呼び出し側が最新の version を渡した場合も、先の更新を消さない。"""
        def other(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.update_note(self.conn, self.admin, self.nid, 1, body="other")
        service.hooks.before_write = other
        service.update_note(self.conn, self.alice, self.nid, 2, title="mine")
        n = self.note()
        self.assertEqual((n["title"], n["body"], n["version"]), ("mine", "other", 3))

    def test_permission_is_checked_again_inside_the_write(self):
        """判定の後に移管されたら、元の所有者はもう更新・削除できない。"""
        def transfer(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        service.hooks.before_write = transfer
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.alice, self.nid, 2, body="x")
        self.assertEqual(self.conn.execute("SELECT body, owner_id FROM notes").fetchone()[:], ("b", 2))

    def test_update_validates_like_create(self):
        for kw in (dict(title=""), dict(title="a" * 101), dict(body="b" * 10001), dict(tags=["BAD"]),
                   dict(tags=["a"] * 6), dict(tags="abc"), dict(tags=["abc\n"])):
            with self.subTest(**{k: repr(v)[:20] for k, v in kw.items()}), self.assertRaises(ValidationError):
                service.update_note(self.conn, self.alice, self.nid, 1, **kw)
        n = self.note()
        self.assertEqual((n["version"], n["tags"]), (1, ["a"]))

    def test_update_failure_leaves_nothing(self):
        before = self.count("audit")
        service.hooks.before_write = lambda step: None
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, self.nid, 5, tags=["z"])
        self.assertEqual((self.note()["tags"], self.count("audit")), (["a"], before))


class DeleteTest(Base):
    def setUp(self):
        super().setUp()
        self.nid = service.create_note(self.conn, self.alice, "t", "b", ["a", "b"])

    def test_owner_and_admin_can_delete(self):
        service.delete_note(self.conn, self.alice, self.nid)
        other = service.create_note(self.conn, self.alice, "t", "b")
        service.delete_note(self.conn, self.admin, other)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (0, 0))

    def test_others_cannot_delete(self):
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.bob, self.nid)
        with self.assertRaises(NotFound):
            service.delete_note(self.conn, self.eve_admin, self.nid)
        self.assertEqual(self.count("notes"), 1)

    def test_failure_rolls_back_tags_and_note_and_is_reported(self):
        self.fail_at("delete:note")
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, self.nid)
        self.assertEqual((self.count("notes"), self.count("note_tags")), (1, 2))

    def test_permission_is_checked_inside_the_delete(self):
        service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        with self.assertRaises(Forbidden):
            service.delete_note(self.conn, self.alice, self.nid)
