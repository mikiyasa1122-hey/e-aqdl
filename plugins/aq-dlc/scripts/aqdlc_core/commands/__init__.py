"""CLI のコマンドの実装（cli.py から段階的に移す）。ここにはコマンドが共通で使う関数を置く。

cli.py から本文を変えずに移した。"""

from ..project import resolve_project
from ..state import require_state


# ------------------------------------------------------------------ helpers
def open_state(args):
    project = resolve_project(args)
    state, spath = require_state(project)
    return project, state, spath
