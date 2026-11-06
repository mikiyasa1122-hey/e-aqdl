"""spec コマンド（仕様の提示・承認・確認）。判断シートの規則は decision_sheet。

cli.py から本文を変えずに移した。"""

from .. import approval as AP
from ..state import (Ctx, bump, last_human_seq, log, problems,
                    save_state, spec_sheet)
from ..util import (die, info, now_iso, pop_flag, pop_opt, short)
from . import open_state


# ------------------------------------------------------------------ orchestrator
def cmd_spec(args):
    present = pop_flag(args, "--present")
    approve = pop_opt(args, "--approve")
    project, s, spath = open_state(args)
    ctx = Ctx(project, s)
    sp = s["spec"]
    if approve is None and not present:
        print(spec_sheet(s), end="")
        st = "未登録" if not sp.get("registered_hash") else (
            "承認済み" if sp.get("approved_hash") == ctx.spec_hash else
            "提示済み・承認待ち" if sp.get("presented_hash") == ctx.spec_hash else "未提示")
        print(f"\n（仕様 {short(ctx.spec_hash)}: {st}）")
        return
    probs = [p for p in problems(ctx) if p["code"] in ("dup_ids", "spec_missing", "graph_incomplete",
                                                         "spec_unregistered", "questions_open", "answers_unapplied")]
    if probs:
        die("仕様を提示・承認できる状態ではありません: " + probs[0]["msg"])
    if present:
        print(spec_sheet(s), end="")
        from .. import workflow as WF
        if WF.config_mode(ctx.cfg) != "full":
            print(f"\n{WF.describe(s, ctx.cfg)}\n（工程を上げる・下げるときは人間に確認する。下げるには「承認 {WF.mode_target(s, 'lite')}」"
                  "などの返答が必要）")
        sp["presented_hash"] = ctx.spec_hash
        sp["presented_seq"] = bump(s)
        save_state(spath, s)
        t = AP.target_id("SPEC", ctx.spec_hash)
        from ..state import pending_by_sheet
        open_q, manual = pending_by_sheet(s)
        sheet = ""
        if open_q or manual:
            sheet = ("判断シートの項目も同じ返答で決めてもらう。質問は推奨でよければ何も書かなくてよい（承認で推奨に決まる）。"
                     "変える質問は「Q1 は B」のように書いてもらう（そのときは承認ではなく修正として扱う）。"
                     + (f"確認しないで進める検証は「承認 DEFER-<ID>」を同じ返答に含めてもらい、承認の記録の後に defer で記録する。" if manual else ""))
        print(f"\n（仕様 {t} を提示として記録。人間に「この仕様で進める（承認 {t}）」/「修正したい」を選んでもらい、"
              f"承認の返答を得たら spec --approve \"<返答の要約>\"。{sheet}"
              "人間への確認は、この1回にまとめる）")
        return
    if sp.get("presented_hash") != ctx.spec_hash:
        die("この版の仕様はまだ人間に提示されていません。spec --present で提示してから確認してください。")
    if last_human_seq(s) <= sp.get("presented_seq", 0):
        die("提示の後に人間の返答が記録されていません。AskUserQuestion などで人間に確認し、返答を得てから承認してください。")
    if not approve.strip():
        die("--approve には人間の返答の要約を書いてください。")
    target = AP.target_id("SPEC", ctx.spec_hash)
    decision, ev, _f = AP.find_decision(s, target, sp.get("presented_seq", 0))
    if decision is None:
        die(f"提示の後の人間の返答に、この仕様（{target}）への明示的な承認がありません。"
            f"「承認 {target}」を選択肢に含めて確認し直してください（「OK」などの対象のない返答は承認にしません）。")
    if decision == "reject":
        AP.record(s, target, "reject", ev, ctx.rd, approve, "spec")
        save_state(spath, s)
        die(f"人間は仕様（{target}）を却下しました。修正の要望を aq-dlc:requirements に依頼してください。")
    from ..state import pending_by_sheet, recommended_option
    open_q, _manual = pending_by_sheet(s)
    from ..decision_sheet import reply_conflicts
    conflicts = reply_conflicts((ev or {}).get("text") or "", open_q)
    if conflicts:
        die("承認の返答が、判断シートの推奨と違う回答かもしれないため、承認として扱いません（"
            + "；".join(conflicts) + "）。answer で回答を記録し、aq-dlc:requirements に反映を依頼してから、提示し直してください。")
    AP.record(s, target, "approve", ev, ctx.rd, approve, "spec")
    for q in open_q:
        # 判断シートの質問は、仕様の承認で推奨に決まる（仕様は推奨を前提に書かれているので、反映済みとして扱う）
        q.update(status="answered", answer=recommended_option(q), answered_by="spec_approval",
                 answered_seq=sp.get("registered_seq", 0), answered_at=now_iso())
        log(s, project, "answer", {"id": q["id"], "answer": q["answer"], "by": "spec_approval"})
    sp.update({"approved_hash": ctx.spec_hash, "approved_seq": bump(s), "approved_at": now_iso(), "note": approve,
               "approval_target": target})
    s["awaiting_human"] = False
    log(s, project, "spec.approve", {"hash": ctx.spec_hash, "target": target, "note": approve})
    save_state(spath, s)
    from .. import workflow as WF
    wf = WF.effective(s, ctx.cfg)
    s.setdefault("workflow_plan", {}).update(at_approval={"mode": wf["mode"], "source": wf["source"],
                                                          "reasons": wf["reasons"], "spec_hash": ctx.spec_hash})
    save_state(spath, s)
    nxt = "設計（aq-dlc:design）" if wf["stages"]["design"] else "実装（aq-dlc:implementer。この工程では設計担当を使わない）"
    info(f"仕様 {short(ctx.spec_hash)} の承認を記録しました。{WF.describe(s, ctx.cfg)}。次は{nxt}です。")
