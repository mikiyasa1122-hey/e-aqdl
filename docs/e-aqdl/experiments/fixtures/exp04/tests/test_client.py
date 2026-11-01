import io
import json
import unittest

from inventory.client import fetch_items


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchItemsTest(unittest.TestCase):
    def test_parses_items(self):
        body = json.dumps({"items": [{"sku": "A1", "name": "ペン", "stock": "3"}]}).encode()
        items = fetch_items("http://x", opener=lambda u, timeout: FakeResp(body))
        self.assertEqual(items, [{"sku": "A1", "name": "ペン", "stock": 3}])
