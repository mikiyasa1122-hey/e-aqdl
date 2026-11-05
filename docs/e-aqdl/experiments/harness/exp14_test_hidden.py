"""実験14 の隠し受け入れテスト（作り手には見せない）。作業フォルダをカレントにして実行する。"""
import csv
import importlib
import io
import os
import pkgutil
import sys
import unittest

sys.path.insert(0, os.getcwd())


def find():
    import inventory
    for m in pkgutil.iter_modules(inventory.__path__):
        mod = importlib.import_module(f"inventory.{m.name}")
        if hasattr(mod, "export_low_stock_csv"):
            return mod.export_low_stock_csv
    if hasattr(inventory, "export_low_stock_csv"):
        return inventory.export_low_stock_csv
    raise AssertionError("export_low_stock_csv が見つからない")


f = find()


def it(sku, stock, name=None):
    return {"sku": sku, "name": name or f"商品{sku}", "stock": stock}


class Hidden(unittest.TestCase):
    def test_filter_and_order(self):
        out = f([it("B", 3), it("A", 3), it("C", 12), it("D", 1), it("E", 10)])
        self.assertEqual(out.split("\n")[0], "sku,name,stock")
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual([r[0] for r in rows[1:] if r], ["D", "A", "B"])  # 10 は未満ではない

    def test_newline_is_lf(self):
        out = f([it("A", 1)])
        self.assertNotIn("\r", out)

    def test_header_only(self):
        out = f([it("A", 50)])
        self.assertEqual(out.rstrip("\n"), "sku,name,stock")

    def test_quoting_roundtrip(self):
        name = '箱入り, "特大" サイズ'
        rows = list(csv.reader(io.StringIO(f([it("A", 1, name)]))))
        self.assertEqual(rows[1], ["A", name, "1"])

    def test_threshold_param(self):
        rows = list(csv.reader(io.StringIO(f([it("A", 4), it("B", 5)], threshold=5))))
        self.assertEqual([r[0] for r in rows[1:] if r], ["A"])

    def test_non_int_stock(self):
        with self.assertRaises(ValueError):
            f([it("A", "3")])
        with self.assertRaises(ValueError):
            f([it("A", 2.5)])

    def test_negative_threshold(self):
        with self.assertRaises(ValueError):
            f([it("A", 1)], threshold=-1)


if __name__ == "__main__":
    unittest.main()
