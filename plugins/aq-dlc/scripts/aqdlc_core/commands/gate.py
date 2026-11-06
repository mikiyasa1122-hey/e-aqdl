"""gate コマンド（前提条件・ロック・保存・表示）。実行の手順は gate_service、合否は verdict。

cli.py から本文を変えずに移した。"""
import datetime as _dt
import os
import time

from .. import gate as G
from ..project import (config, register_active)
from ..state import (Ctx, bump, id_index,
                    iter_verifications, load_state, problems,
                    running_agents, save_state)
from ..util import (die, info, now_iso, pop_flag, write_text)
from . import open_state


def cmd_gate(args):
    after_merge = pop_flag(args, "--after-merge")
    force = pop_flag(args, "--force")
    project, s, spath = open_state(args)
    cfg = config(project)
    ctx = Ctx(project, s, cfg)
    _i, dups = id_index(s)
    if dups:
        die(f"IDが重複しているため gate を実行できません（結果が上書きされる）: {', '.join(dups)}")
    from ..state import actor_hint
    actor = actor_hint(project)
    run = running_agents(s)
    if run and not force and not (actor == "implementer" and all(v["role"] == "implementer" for v in run.values())):
        die("サブエージェントの実行中は gate を実行しません（同じフォルダでテストが衝突するため）。完了を待ってください。")
    spec_block = [p for p in problems(ctx) if p["code"] in ("spec_missing", "spec_unpresented", "spec_unapproved",
                                                              "questions_open", "spec_unregistered", "memory_scope",
                                                              "state_invalid")]
    if spec_block and not force:
        die(f"{spec_block[0]['msg']}。仕様の承認前は gate を実行しません（実装の前に仕様をすり合わせるため）。")
    lock = os.path.join(ctx.rd, "gate.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3600 and not force:
        die("別の gate が実行中です（終わってから再実行。前回が異常終了した場合は --force）。")
    write_text(lock, str(os.getpid()))
    from .. import gate_service as GS
    try:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        info(f"gate {stamp}  ({project})")
        out = GS.run(project, s, cfg, ctx.rd, stamp, iter_verifications, after_merge, lambda: config(project))
        for ln in out["lines"]:
            print(ln)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    s, spath = load_state(project)  # gate 実行中に他の記録が入っていてもよいように読み直す
    before, after, results, ok, dur = out["before"], out["after"], out["results"], out["ok"], out["sec"]
    modified = GS.modified_during(before, after, out["fps"], out["cfg_after"], project,
                                  [v for _r, _p, v in iter_verifications(s)])
    if modified:
        ok = False  # 検証したものと結果を紐付けるものが一致しないため不合格
    s["gate"] = {"seq": bump(s), "stamp": stamp, "ran_at": now_iso(), "after_merge": after_merge, "sec": dur,
                 "code_fp": G.code_fp(before), "code_fp_after": G.code_fp(after), "snapshot": before,
                 "checks_fp": out["fps"]["checks"], "config_fp": out["fps"]["config"], "graph_fp": ctx.graph_fp,
                 "checks_pass": ok, "results": results, "holdout_fp": out["holdout_fp"],
                 "modified_during_gate": modified}
    s.setdefault("gate_history", []).append({"stamp": stamp, "pass": ok, "sec": dur, "after_merge": after_merge,
                                             "modified": bool(modified)})
    if after_merge:
        s["after_merge_gate"] = True
    s["stop_blocks"] = 0
    save_state(spath, s)
    register_active(project, s["run_id"])
    if not cfg.get("checks"):
        print("  ! チェックが登録されていません（完了条件を満たせません）")
    if modified:
        print(f"  ! gate の実行中に変更がありました: {modified['summary']}")
        print("    検証したものと結果を紐付けるものが一致しないため、gate を不合格にしました。"
              "テストやビルドが追跡対象のファイルを書き換えていないか確認してください。")
        log_dir = os.path.join(ctx.rd, "gate")
        write_text(os.path.join(log_dir, f"{stamp}-modified.log"),
                   "\n".join(["gate の実行中に変わったもの"] + [f"変更: {x}" for x in modified["changed"]]
                             + [f"追加: {x}" for x in modified["added"]] + [f"削除: {x}" for x in modified["removed"]]
                             + [f"定義: {x}" for x in modified["definitions"]]) + "\n")
    info(f"gate 結果: {'合格' if ok else '不合格'}")
