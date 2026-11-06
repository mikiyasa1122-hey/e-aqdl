"""Claude Code の hook の入口（hooks.json の `aqdlc.py hook <名前>` から呼ばれる）。

hook の名前から処理を選び、入力（JSON）を読み、結果（JSON）を出す。許可してよいと確定できないときは
拒否する（FAIL_CLOSED）。各 hook の処理は同じパッケージの pre_tool・post_tool・session・subagent にある。

cli.py から本文を変えずに移した。"""
import re
import sys

from .. import agents as A
from .. import gitops
from ..state import StateError
from .common import _audit_hook, _hook_project, _record_hook_error, emit, read_payload
from .post_tool import hook_post_ask, hook_post_bash
from .pre_tool import hook_pre_agent, hook_pre_bash, hook_pre_read, hook_pre_write
from .session import hook_session_start, hook_stop, hook_user_prompt
from .subagent import hook_subagent_start, hook_subagent_stop


HOOKS = {
    "session-start": hook_session_start, "user-prompt": hook_user_prompt, "pre-bash": hook_pre_bash,
    "pre-write": hook_pre_write, "pre-agent": hook_pre_agent, "pre-read": hook_pre_read, "post-ask": hook_post_ask,
    "post-bash": hook_post_bash,
    "subagent-start": hook_subagent_start, "subagent-stop": hook_subagent_stop, "stop": hook_stop,
}


LEGACY_HOOKS = {"stop-hook": "stop", "guard-hook": "pre-bash", "session-hook": "session-start",
                "agent-start-hook": None, "agent-stop-hook": None}


# 許可してよいと確定できないときは拒否する hook（PreToolUse）
FAIL_CLOSED = {"pre-bash", "pre-write", "pre-agent"}


RECOVERY_CMD = re.compile(r"^\s*(\S*python3?\S*\s+)?[\"']?\S*aqdlc\.py[\"']?\s+(new|status|doctor|audit)\b[^;&|`$()<>]*$")


def run_hook(name):
    payload = read_payload()
    fn = HOOKS.get(name)
    if not fn:
        return
    if payload is None:
        # 入力を読めない。重要な PreToolUse は、E-AQDL のプロジェクトの中なら許可しない
        _audit_hook(name, None, "hook の入力（JSON）を読めない")
        if name in FAIL_CLOSED and _hook_project({}):
            emit(A.deny("hook の入力を読めなかったため、安全のため止めました。もう一度実行してください。"))
        return
    if name not in FAIL_CLOSED and name != "post-bash":
        # 重要な PreToolUse は偽装の判定をしない（偽装しても許可されることはないため、常に通常の判定をする）
        if A.forged_hook(_hook_project(payload), name):
            print("[E-AQDL] Bash から hook を直接呼ぶことはできません。", file=sys.stderr)
            return
    try:
        emit(fn(payload))
    except SystemExit as e:
        # 状態・設定を読めない（die）など。重要な PreToolUse は許可せずに止める
        if name in FAIL_CLOSED and e.code not in (0, None) and _hook_project(payload):
            _audit_hook(name, payload, f"処理が中断された（終了コード {e.code}）")
            emit(A.deny("状態・設定を読めないため、許可してよいか確定できず止めました。"
                        "state.json・config.json が壊れていないか人間に確認してください。"))
            return
        raise
    except Exception as e:  # noqa: BLE001
        _audit_hook(name, payload, e)
        if name == "pre-bash" and isinstance(e, StateError) and RECOVERY_CMD.match(
                (payload.get("tool_input") or {}).get("command", "") or ""):
            return  # 状態を読めないときの復旧（new --force・status・doctor・audit）だけは許可する
        if name == "post-bash":
            _record_hook_error(payload, e)
            return
        if name in FAIL_CLOSED:
            cmd = (payload.get("tool_input") or {}).get("command", "") or ""
            if _hook_project(payload) or gitops.dangerous_words(cmd):
                emit(A.deny(f"判定の処理でエラーが発生し、許可してよいか確定できないため止めました（{e}）。"
                            "状況を人間に報告してください（監査ログ: ~/.aq-dlc/audit.jsonl）。"))
                return
        print(f"[E-AQDL] hook（{name}）でエラーが発生しました（作業は継続します）: {e}", file=sys.stderr)
