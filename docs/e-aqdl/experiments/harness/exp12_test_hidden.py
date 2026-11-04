"""実験12 の隠し受け入れテスト。SERVER_DIR（成果物）を import パスに入れて、inventory から関数を探す。"""
import importlib
import os
import pkgutil
import sys
import unittest

APP = os.environ["APP_DIR"]
sys.path.insert(0, APP)
import inventory  # noqa: E402


def find(name):
    if hasattr(inventory, name):
        return getattr(inventory, name)
    for m in pkgutil.iter_modules(inventory.__path__):
        mod = importlib.import_module("inventory." + m.name)
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AssertionError(f"{name} が見つからない")


def it(sku, stock):
    return {"sku": sku, "name": sku.lower(), "stock": stock}


class TotalStock(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(find("total_stock")([it("A1", 3), it("B2", 0), it("C3", 12)]), 15)

    def test_empty(self):
        self.assertEqual(find("total_stock")([]), 0)

    def test_does_not_modify(self):
        items = [it("A1", 3)]
        find("total_stock")(items)
        self.assertEqual(items, [it("A1", 3)])


class ValidSku(unittest.TestCase):
    def test_valid(self):
        f = find("is_valid_sku")
        for s in ("A1", "AB123", "ABC123456", "Z9"):
            self.assertIs(f(s), True, s)

    def test_invalid(self):
        f = find("is_valid_sku")
        for s in ("", "1", "A", "ABCD1", "A1234567", "ab12", "AB-12", "AB12 ", " AB12", "AB12\n", "Ａ1", "A１"):
            self.assertIs(f(s), False, repr(s))

    def test_non_string(self):
        f = find("is_valid_sku")
        for s in (None, 123, b"AB12", ["AB12"]):
            self.assertIs(f(s), False, repr(s))


class LowStock(unittest.TestCase):
    def test_default_threshold_boundary(self):
        items = [it("A1", 10), it("B2", 9), it("C3", 0), it("D4", 11)]
        self.assertEqual(find("low_stock")(items), [it("B2", 9), it("C3", 0)])

    def test_custom_threshold_and_order(self):
        items = [it("A1", 5), it("B2", 1), it("C3", 4)]
        self.assertEqual(find("low_stock")(items, threshold=5), [it("B2", 1), it("C3", 4)])
        self.assertEqual(find("low_stock")(items, 5), [it("B2", 1), it("C3", 4)])

    def test_empty(self):
        self.assertEqual(find("low_stock")([]), [])


if __name__ == "__main__":
    unittest.main()
