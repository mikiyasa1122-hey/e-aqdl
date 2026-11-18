"""V8: service.py に文字列連結・書式による SQL 組み立てがないことの機械確認。"""
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "notes" / "service.py"
EXEC_NAMES = {"execute", "executemany", "executescript"}


def _bad_reason(node: ast.expr, consts: dict[str, ast.expr]) -> str | None:
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    if isinstance(node, ast.BinOp):
        return "演算子による連結・書式"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return ".format"
    if isinstance(node, ast.Name):  # 固定の定数名なら中身を調べる
        target = consts.get(node.id)
        return _bad_reason(target, consts) if target is not None else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):  # 固定 SQL の辞書引き
        target = consts.get(node.value.id)
        if isinstance(target, ast.Dict):
            for v in target.values:
                r = _bad_reason(v, consts)
                if r:
                    return r
        return None
    return None


def find_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    consts: dict[str, ast.expr] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            consts[n.targets[0].id] = n.value
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in EXEC_NAMES:
            if not n.args:
                out.append(f"line {n.lineno}: 引数なし")
                continue
            r = _bad_reason(n.args[0], consts)
            if r:
                out.append(f"line {n.lineno}: {r}")
    return out


class NoSqlConcatTest(unittest.TestCase):
    def test_serviceにexecute呼び出しがある(self) -> None:
        self.assertGreater(SRC.read_text(encoding="utf-8").count(".execute("), 10)

    def test_service_pyに文字列連結SQLがない(self) -> None:
        self.assertEqual(find_violations(SRC.read_text(encoding="utf-8")), [])

    def test_検出器自体が違反を検出できる(self) -> None:
        for bad in ['conn.execute(f"SELECT {x}")', 'conn.execute("SELECT " + x)',
                    'conn.execute("SELECT %s" % x)', 'conn.execute("SELECT {}".format(x))',
                    'Q = "a" + x\nconn.execute(Q)']:
            with self.subTest(bad):
                self.assertTrue(find_violations(bad))
        self.assertEqual(find_violations('conn.execute("SELECT ?", (1,))'), [])
