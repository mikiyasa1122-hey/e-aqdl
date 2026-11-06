"""PostToolUse / PostToolUseFailure の hook（Bash の後の変更の検出・質問の記録）。

cli.py から本文を変えずに移した。"""
import os
import shutil

from .. import agents as A
from .. import gate as G
from .. import approval as AP
from ..project import config
from ..state import (ROLE_LABEL, bump, human_event, load_state, run_dir, save_state)
from ..util import (load_json, now_iso)
from .common import hook_context


def hook_post_bash(payload):
    """実装担当以外の Bash がコード・テスト・設定を変えていないか。

    リダイレクト・スクリプト・変数展開などの書き方に関係なく、実行前後のファイル内容で判定する。
    """
    role = A.role_of(payload.get("agent_type"))
    cwd, project, state, spath = hook_context(payload)
    if not project:
        return None
    forged = _check_forged_human(project, payload)
    if role == "implementer":
        return forged
    res = _post_bash_changes(payload, role, project, state)
    if forged and res:
        forged["reason"] += "\n" + res["reason"]
        forged["hookSpecificOutput"]["additionalContext"] = forged["reason"]
    return forged or res


def _post_bash_changes(payload, role, project, state):
    snap_path = A.bash_snapshot_path(project, payload.get("tool_use_id"))
    pre = load_json(snap_path, None)
    active = bool(state and state.get("status") in ("in_progress", "pr_ready"))
    cfg = config(project)
    if pre is None:
        if not active:
            return None
        v = {"role": role, "at": now_iso(), "cmd": str((payload.get("tool_input") or {}).get("command", ""))[:500],
             "files": [], "count": 0, "kind": "tamper",
             "note": "実行前の記録が見つからない（記録の削除・改ざんの可能性）"}
    else:
        notes = []
        now_fp = G.config_fp(cfg, project)
        if pre.get("config_fp") and pre["config_fp"] != now_fp and not (state and state.get("config_fp_registered") == now_fp):
            # エンジンのコマンド（add-check など）で登録された変更は除く
            notes.append("設定（config.json または .claude の設定・定義）が変更された")
        files = []
        if pre.get("snapshot") is not None:
            # 実行前と同じ対象で比べる（設定で対象を外す改ざんをしても、ファイルの変更は見える）
            same = dict(cfg, gate=dict(cfg["gate"], ignore_dirs=pre.get("ignore_dirs", cfg["gate"]["ignore_dirs"])))
            # 変更の比較は ctime・inode 付きのキャッシュで十分（utime で ctime は戻せない）。
            # 全ファイルのハッシュのし直しは、判定と登録の時点で行う（hook の時間制限を超えないため）
            # Windows の ctime は作成時刻で書き換えを表さないため、Windows では全ファイルをハッシュし直す
            after = G.snapshot(project, same, pre["snapshot"], fresh=(os.name == "nt"))
            c, a, r = G.snapshot_diff(pre["snapshot"], after)
            files = c + a + r
        if (not files and not notes) or (not active and not notes):
            try:
                os.remove(snap_path)
            except OSError:
                pass
            return None
        v = A.detect_bash_changes(role, pre.get("cmd", ""), {}, {}, project) or \
            {"role": role, "at": now_iso(), "cmd": pre.get("cmd", "")[:500], "kind": "write"}
        v.update(files=files[:50], count=len(files))
        if A.GIT_SYNC.search(pre.get("cmd", "")):
            v["kind"] = "git"
        if notes and not files:
            v["kind"] = "config"
        if notes:
            v["note"] = "、".join(notes)
    try:
        os.remove(snap_path)
    except OSError:
        pass
    state, spath = load_state(project)
    if not state:
        return None
    v["seq"] = bump(state)
    if v["kind"] == "config":
        v["resolved"] = {"by": "config_changed で人間が確認する"}  # 設定の変更は config_changed（人間の確認）で扱う
    state.setdefault("violations", []).append(v)
    save_state(spath, state)
    who = ROLE_LABEL.get(role, "統括")
    detail = f"{v['count']} 件: {', '.join(v['files'][:5])}" if v.get("files") else v.get("note", "")
    reason = (f"E-AQDL: 担当外の変更を検出しました（{who} の Bash: {detail}）。"
              "run の実行中にコードやテストを変えられるのは実装担当だけで、設定はエンジンのコマンドでだけ変えます。"
              "この変更は記録され、実装担当の確認・取り込み（設定なら人間の確認）と gate・独立レビューのやり直しが"
              "済むまで、レビュー・完了に進めません。")
    if v["kind"] == "git":
        reason += "（git の操作による作業ツリーの変化も、取り込みの確認が必要です）"
    return {"decision": "block", "reason": reason,
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reason}}


def _check_forged_human(project, payload):
    """Bash の実行中に記録された「人間の返答」は、hook を直接呼んだ偽装として無効にする。

    人間の発言（UserPromptSubmit）と AskUserQuestion の回答は、Bash の実行中には届かない。実行中に増えた
    返答は、承認の判定に使わないようにし、それに基づく承認を取り消す。
    """
    path = A.bash_human_path(project, payload.get("tool_use_id"))
    pre = load_json(path, None)
    try:
        os.remove(path)
    except OSError:
        pass
    if not pre:
        return None
    state, spath = load_state(project)
    if not state or state.get("run_id") != pre.get("run"):
        return None
    bad = [e for e in state.get("human_events", []) if e.get("seq", 0) > pre["seq"] and e.get("kind") != "forged"]
    if not bad:
        return None
    seqs = {e["seq"] for e in bad}
    for e in bad:
        e.update(kind="forged", parsed=False)
    revoked = set()
    for a in list(state.get("approvals", [])):
        if a.get("event_seq") in seqs and a.get("decision") == "approve":
            state["approvals"].append(dict(a, decision="revoked", at=now_iso(), note="Bash の実行中に記録された返答による承認"))
            revoked.add(a["target"])
    sp = state.get("spec") or {}
    if sp.get("approval_target") in revoked:
        for k in ("approved_hash", "approved_seq", "approved_at", "approval_target"):
            sp.pop(k, None)
    ms = state.get("memory_scope") or {}
    if ms.get("event_seq") in seqs or (ms.get("seq", 0) > pre["seq"] and ms.get("source") in ("approval", "user_reply")):
        state.pop("memory_scope", None)
    # 取り消せる操作は元に戻す（設定の受け入れ・等価の除外・共通 Memory への共有）
    for h in reversed(state.get("history", [])):
        if h.get("action") == "config.accept" and h.get("seq", 0) > pre["seq"] and \
                AP.target_id("CFG", (h.get("detail") or {}).get("after")) in revoked:
            state["config_fp_registered"] = h["detail"].get("before")
    cfst = state.get("counterfactual") or {}
    for k, v in list((cfst.get("equivalent") or {}).items()):
        if v.get("approval") in revoked:
            cfst["equivalent"].pop(k)
    for m in ((cfst.get("latest") or {}).get("mutations") or []):
        if (m.get("equivalent") or {}).get("approval") in revoked:
            m.pop("equivalent")
    for sid, sh in ((state.get("memory") or {}).get("share") or {}).items():
        if sid in revoked and sh.get("status") == "shared" and sh.get("dest"):
            from ..project import home as _home
            q = os.path.join(_home(), "memory-quarantine", os.path.basename(sh["dest"]) + f".{sid}")
            try:
                os.makedirs(os.path.dirname(q), exist_ok=True)
                shutil.move(sh["dest"], q)
            except OSError:
                pass
            sh.update(status="revoked", quarantined=q)
            AP.audit_append(os.path.join(_home(), "memory", "audit.jsonl"), "memory.share.revoked", share=sid,
                            quarantined=q)
    from ..state import iter_verifications as _iv
    for _r, _p, v in _iv(state):
        if v.get("confirmed_by") == "human" and f"VERIFY-{v['id']}" in revoked:
            for k in ("confirmed_by", "confirm_note", "confirmed_seq"):
                v.pop(k, None)
        if v.get("deferred") and f"DEFER-{v['id']}" in revoked:
            v.pop("deferred", None)
    fid = f"FORGED-{bump(state)}"
    state.setdefault("forged_events", []).append({"id": fid, "seqs": sorted(seqs), "role": pre.get("role"),
                                                  "cmd": pre.get("cmd"), "at": now_iso(), "revoked": sorted(revoked),
                                                  "resolved": False})
    save_state(spath, state)
    rd = run_dir(project, state.get("run_id"))
    AP.audit_append(os.path.join(rd, "audit.jsonl"), "human.forged", id=fid, seqs=sorted(seqs),
                    revoked=sorted(revoked), cmd=pre.get("cmd"))
    reason = (f"E-AQDL: Bash の実行中に「人間の返答」が記録されました（{fid}）。hook を直接呼んだ偽装として無効にし、"
              f"それに基づく承認（{', '.join(sorted(revoked)) or 'なし'}）を取り消しました。人間に状況を報告してください。")
    return {"decision": "block", "reason": reason,
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reason}}


def hook_post_ask(payload):
    if A.role_of(payload.get("agent_type")) != "orchestrator":
        return None
    cwd, project, state, spath = hook_context(payload)
    if state and state.get("status") in ("in_progress", "pr_ready"):
        text, parsed = AP.parse_ask_response(payload.get("tool_response"))
        human_event(state, "answer", text, parsed)
        save_state(spath, state)
    return None
