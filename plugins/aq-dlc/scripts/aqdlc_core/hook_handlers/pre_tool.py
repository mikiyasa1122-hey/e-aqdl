"""PreToolUse の hook（Bash・書き込み・読み取り・サブエージェントの起動の許可／拒否）。

cli.py から本文を変えずに移した。"""
import os
import re
import time

from .. import agents as A
from .. import gate as G
from .. import gitops
from ..project import (config, find_project)
from ..state import (load_state, run_dir)
from ..util import save_json
from .common import hook_context


def hook_pre_bash(payload):
    cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    cwd, project, state, spath = hook_context(payload)
    role = A.role_of(payload.get("agent_type"))
    if not project:
        for m in re.finditer(r"(?:cd|-C|--project)\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd):
            cand = find_project(os.path.expanduser(m.group(1).strip("\"'")))
            if cand:
                project = cand
                state, spath = load_state(project)
                break
    if not project:
        return None
    cfg = config(project)
    if state:
        rd = run_dir(project, state.get("run_id"))
        if rd:
            save_json(os.path.join(rd, "actor.json"), {"role": role, "at": time.time(), "cmd": cmd[:8000]})
    why = A.check_bash(role, cmd, project, state, cfg)
    if why:
        return A.deny(why)
    if state:
        # すべての担当の Bash について、実行前の seq を記録する（実行中に「人間の返答」が増えたら偽装として扱う）
        save_json(A.bash_human_path(project, payload.get("tool_use_id")),
                  {"seq": int(state.get("seq", 0)), "run": state.get("run_id"), "role": role, "cmd": cmd[:500]})
    # 実装担当以外の Bash は、実行前のコードと設定の状態をプロジェクトの外に記録し、実行後に比べる。
    # run がない間も「記録した」こと自体は残す（実行後に記録が消えていたら改ざんとして扱うため）。
    if role != "implementer":
        sdir = A.bash_snapshot_dir(project)
        if os.path.isdir(sdir):  # 対応する PostToolUse が来なかった古い記録を掃除する
            for f in os.listdir(sdir):
                fp = os.path.join(sdir, f)
                try:
                    if time.time() - os.path.getmtime(fp) > 3600:
                        os.remove(fp)
                except OSError:
                    pass
        active = bool(state and state.get("status") in ("in_progress", "pr_ready"))
        prev = (state.get("gate") or {}).get("snapshot") if state else None
        save_json(A.bash_snapshot_path(project, payload.get("tool_use_id")),
                  {"role": role, "cmd": cmd[:2000], "active": active,
                   "snapshot": G.snapshot(project, cfg, prev) if active else None,
                   "ignore_dirs": list(cfg["gate"]["ignore_dirs"]), "config_fp": G.config_fp(cfg, project)})
    if re.search(r"\b(git|gh)\b|api\.github\.com", cmd):
        try:
            reasons = gitops.guard_reasons(cmd, project, gitops.current_branch, gitops.remote_has_branch)
        except Exception as e:  # noqa: BLE001
            if gitops.dangerous_words(cmd):
                return A.deny(f"ガードの判定に失敗したため、安全のため止めました（{e}）。人間に確認してください。")
            reasons = []
        if reasons:
            return A.deny("ガード: " + " / ".join(dict.fromkeys(reasons)))
    return None


def hook_pre_write(payload):
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    cwd, project, state, spath = hook_context(payload)
    if not project or not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    project = os.path.realpath(project)
    why = A.check_write(A.role_of(payload.get("agent_type")), path, project, state, config(project))
    return A.deny(why) if why else None


def hook_pre_read(payload):
    """Read・Grep・Glob の前: 実装担当は隠し受け入れテストを、ブラインドレビューのレビュー担当は実装報告を読めない。"""
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
    cwd, project, state, spath = hook_context(payload)
    if not project:
        return None
    role = A.role_of(payload.get("agent_type"))
    if not path:
        # パス指定のない Grep・Glob はプロジェクト全体が対象（holdout はプロジェクトの外なので含まれない）
        path = cwd
    elif not os.path.isabs(path):
        path = os.path.join(cwd, path)
    why = A.check_read(role, path, os.path.realpath(project), state, config(project))
    if not why and ti.get("pattern") and isinstance(ti.get("pattern"), str) and os.path.isabs(ti["pattern"]):
        why = A.check_read(role, ti["pattern"], os.path.realpath(project), state, config(project))
    return A.deny(why) if why else None


def hook_pre_agent(payload):
    cwd, project, state, spath = hook_context(payload)
    if not project:
        role = A.role_of((payload.get("tool_input") or {}).get("subagent_type"))
        if role in A.ROLES:
            return A.deny("E-AQDL プロジェクトではありません。init で初期化し、new で run を開始してください。")
        return None
    return A.on_pre_agent(payload, project, state, spath)
