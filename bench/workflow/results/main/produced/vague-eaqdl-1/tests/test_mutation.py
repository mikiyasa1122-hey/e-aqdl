"""反証（変異）テスト。守りの条件を弱めたとき、対応する検証が実際に失敗することを確かめる（V25 V30）。"""
import unittest
from unittest import mock

from notes import service
from tests.helpers import check_org_isolation, check_update_conflict

# プレースホルダ数を保ったまま条件だけを無効にした版
NO_ORG_VISIBLE_SQL = "(? IS NOT NULL AND ? IS NOT NULL)"
NO_VERSION_UPDATE_SQL = ("UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
                         "WHERE id = ? AND ? IS NOT NULL AND {visible}")


class MutationTest(unittest.TestCase):
    def test_V25_元の実装では組織分離の検証が通る(self):
        check_org_isolation(self)

    def test_V25_組織条件を外すと組織分離の検証が失敗する(self):
        with mock.patch.object(service, "VISIBLE_SQL", NO_ORG_VISIBLE_SQL):
            with self.assertRaises(AssertionError):
                check_org_isolation(self)

    def test_V30_元の実装では更新競合の検証が通る(self):
        check_update_conflict(self)

    def test_V30_version条件を外すと更新競合の検証が失敗する(self):
        with mock.patch.object(service, "UPDATE_SQL", NO_VERSION_UPDATE_SQL):
            with self.assertRaises(AssertionError):
                check_update_conflict(self)


if __name__ == "__main__":
    unittest.main()
