"""hook の共通の処理（入力の読み取り・出力・状態の文脈・hook の異常の記録）。

cli.py から本文を変えずに移した。"""
import json
import os
import sys

from .. import agents as A
from .. import approval as AP
from ..project import (find_project, active_project_for)
from ..state import (bump, load_state, save_state)
from ..util import now_iso


def read_payload():
    """hook の入力。読めなければ None（安全側の判断に使う）。"""
    from ..util import decode
    try:
        data = json.loads(decode(sys.stdin.buffer.read()) or "{}")
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def emit(obj):
    if obj:
        print(json.dumps(obj, ensure_ascii=False))


def hook_context(payload):
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = active_project_for(cwd)
    state, spath = (load_state(project) if project else (None, None))
    tp = payload.get("transcript_path")
    if state and tp and not payload.get("agent_type"):
        changed = False
        if state.get("session_transcript") != tp:
            state["session_transcript"] = tp  # 統括の会話記録（トークンの集計に使う）
            changed = True
        if AP.trusted_transcript(tp) and tp not in state.setdefault("transcripts", []):
            state["transcripts"] = (state["transcripts"] + [tp])[-10:]  # 人間の返答の照合に使う
            changed = True
        if changed:
            save_state(spath, state)
    return cwd, project, state, spath


def _record_hook_error(payload, err):
    """検出の処理で例外が起きたら、見逃さないように「確認が必要な変更」として記録する（安全側）。"""
    try:
        cwd = payload.get("cwd") or os.getcwd()
        project = active_project_for(cwd)
        state, spath = load_state(project) if project else (None, None)
        if state and state.get("status") in ("in_progress", "pr_ready"):
            state.setdefault("violations", []).append({
                "role": A.role_of(payload.get("agent_type")), "at": now_iso(), "seq": bump(state), "kind": "error",
                "cmd": str((payload.get("tool_input") or {}).get("command", ""))[:500], "files": [], "count": 0,
                "note": f"変更の検出処理でエラー: {err}"})
            save_state(spath, state)
    except Exception:  # noqa: BLE001
        pass
    print(f"[E-AQDL] 変更の検出でエラーが発生したため、確認が必要な変更として記録しました: {err}", file=sys.stderr)


def _hook_project(payload):
    cwd = (payload or {}).get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return active_project_for(cwd) or find_project(cwd)
    except (Exception, SystemExit):  # noqa: BLE001
        return None


def _audit_hook(name, payload, why):
    from ..project import home
    AP.audit_append(os.path.join(home(), "audit.jsonl"), "hook.error", hook=name, reason=str(why)[:300],
                    cwd=str((payload or {}).get("cwd", ""))[:300], tool=str((payload or {}).get("tool_name", "")))
