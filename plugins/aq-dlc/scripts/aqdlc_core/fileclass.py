"""ファイルの分類（テストファイル・コードのファイル）。

gate.py（テストの兆候の検出）と quality.py（差分カバレッジの対象）の両方が使う。
ここはパッケージ内の他のモジュールに依存しない（gate ↔ quality の循環を作らないため）。
定義は gate.py にあったものをそのまま移した（gate.TEST_FILE・gate.CODE_EXT の名前も残す）。
"""
import re

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e)/|(_test|\.test|\.spec|Test|Tests)\.[A-Za-z]+$"
                       r"|(^|/)test_[^/]+\.py$")
CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb",
            ".php", ".cs", ".vue", ".svelte")


def is_test(rel):
    return bool(TEST_FILE.search(rel))


def is_code(rel):
    return rel.endswith(CODE_EXT)
