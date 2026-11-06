"""SessionStart・UserPromptSubmit・Stop の hook。

cli.py から本文を変えずに移した。"""
import os
import sys
import time

from ..project import (config, engine_cmd, find_project, unregister_active)
from ..state import (Ctx, human_event, load_state, next_action, problems,
                    running_agents, save_state)
from ..util import now_iso
from .common import hook_context


# ------------------------------------------------------------------ hooks
LANG_RULE = ("【言語】ユーザーへの返答、質問、進捗報告、run.md、Memory、コミットメッセージ、PR のタイトルと本文、"
             "Issue、コード内のコメント、サブエージェントへの依頼と報告は、すべて日本語で書いてください。"
             "英語のエラーやログを伝えるときは、日本語で要約してから必要な部分だけ引用してください。"
             "コードの識別子やコマンドは英語のままで構いません。")


def hook_user_prompt(payload):
    cwd, project, state, spath = hook_context(payload)
    if state and state.get("status") in ("in_progress", "pr_ready"):
        human_event(state, "prompt", payload.get("prompt") or "")
        state["awaiting_human"] = False  # 人間が返答したので、確認待ちを解除する
        save_state(spath, state)
    return None


def hook_stop(payload):
    cwd, project, state, spath = hook_context(payload)
    if not project or not state or state.get("status") in ("aborted", "closed", "pr_ready"):
        return None
    if state.get("awaiting_human") or running_agents(state):
        return None
    w = state.get("waiting")
    if w and time.time() < w.get("until", 0):
        return None
    cfg = config(project)
    ctx = Ctx(project, state, cfg, fresh=state.get("status") == "complete")
    total_cap = int(cfg["automation"].get("max_total_continues", 200))
    if int(state.get("stop_blocks_total", 0)) >= total_cap and state.get("status") != "complete":
        print(f"[E-AQDL] run 全体での自動継続の上限（{total_cap}回）に達したため停止を許可しました。"
              "状況を確認して人間が判断してください。", file=sys.stderr)
        return None
    if state.get("status") == "complete":
        final = problems(ctx, "final")
        if not final:
            state["status"] = "closed"
            state["closed_at"] = now_iso()
            save_state(spath, state)
            unregister_active(project)
            return None
        state["status"] = "in_progress"
    max_blocks = int(cfg["automation"]["max_auto_continues"])
    if state.get("stop_blocks", 0) >= max_blocks:
        state["stop_blocks"] = 0
        save_state(spath, state)
        print(f"[E-AQDL] 自動継続の上限（{max_blocks}回）に達したため停止を許可しました。", file=sys.stderr)
        return None
    phase, nxt, _probs = next_action(ctx)
    state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    state["stop_blocks_total"] = int(state.get("stop_blocks_total", 0)) + 1
    save_state(spath, state)
    reason = (f"E-AQDL: run が進行中です（{project} / フェーズ: {phase}）。\n次: {nxt}\n"
              "人間の判断が必要なら ask を実行してから質問してください。中断するなら abort。\n"
              "（報告や記録はすべて日本語で書いてください）")
    return {"decision": "block", "reason": reason}


def _in_git_repo(path):
    d = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def hook_session_start(payload):
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if os.path.realpath(cwd) in (os.path.realpath(os.path.expanduser("~")), os.path.realpath(os.sep)):
        return None
    project = find_project(cwd)
    if not config(project)["automation"].get("auto_start", True):
        return None
    eng = engine_cmd()
    if project:
        state, _ = load_state(project)
        lines = [f"このフォルダは E-AQDL プロジェクトです（{project}）。",
                 "あなたは E-AQDL の統括（Main Orchestrator）です。開発・修正・機能追加の依頼は、コマンドがなくても "
                 "aq-dlc:start Skill の手順で進めます。要求分析・設計・実装・レビューはサブエージェントに任せ、"
                 "あなたは進行管理・人間への確認・Quality Gate の実行に専念してください。"]
        if state and state.get("status") in ("in_progress", "pr_ready"):
            phase, nxt, _p = next_action(Ctx(project, state))
            lines.append(f"進行中の run: {state['run_id']}（フェーズ: {phase}）。依頼がこの run の続きなら再開してください。")
            lines.append(f"次: {nxt}")
        lines.append(f"エンジン: {eng} <コマンド> --project \"{project}\"")
    else:
        try:
            visible = [e for e in os.listdir(cwd) if not e.startswith(".")]
        except OSError:
            return None
        if visible:
            if not _in_git_repo(cwd):
                return None
            lines = [f"このフォルダ（{cwd}）は git リポジトリです（E-AQDL は未導入）。",
                     "ユーザーが機能の追加・仕様の変更・まとまった不具合修正を依頼したら、コマンドを打たれなくても "
                     "aq-dlc:start Skill を使い、init で E-AQDL を導入してから進めてください（既存のコードは変更の対象、"
                     "変えない部分は回帰を確かめる対象として仕様にする）。",
                     "誤字や1行の修正、質問への回答、調査だけの依頼なら、E-AQDL を使わず通常どおり対応してください。",
                     LANG_RULE]
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}
        lines = [f"このフォルダ（{cwd}）は空です。",
                 "ユーザーが新しいアプリやツールの作成を依頼したら、コマンドを打たれなくても aq-dlc:start Skill を使い、"
                 "このフォルダを E-AQDL プロジェクトとして初期化して開発してください。",
                 "開発以外の質問や雑談なら、通常どおり答えてください。"]
    if sys.platform == "win32":
        lines.append("【Windows】Playwright を使う場合は、ブラウザをプロジェクト内に入れてください"
                     "（PLAYWRIGHT_BROWSERS_PATH=0 を付けて npx playwright install）。")
    lines.append(LANG_RULE)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}
