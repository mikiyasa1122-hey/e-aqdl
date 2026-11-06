"""SubagentStart・SubagentStop の hook（引き継ぎ情報・成果物の検証）。

cli.py から本文を変えずに移した。"""

from .. import agents as A
from .common import hook_context


def hook_subagent_start(payload):
    cwd, project, state, spath = hook_context(payload)
    return A.on_subagent_start(payload, project, state, spath) if project else None


def hook_subagent_stop(payload):
    cwd, project, state, spath = hook_context(payload)
    return A.on_subagent_stop(payload, project, state, spath) if project else None
