"""本物の在庫 API に対する結合テスト（社内ネットワークで実行する）。"""
import unittest

from inventory.client import fetch_items


class LiveInventoryTest(unittest.TestCase):
    def test_live_api_returns_items(self):
        items = fetch_items(timeout=5)
        self.assertGreater(len(items), 0)
        for it in items:
            self.assertIn("sku", it)
