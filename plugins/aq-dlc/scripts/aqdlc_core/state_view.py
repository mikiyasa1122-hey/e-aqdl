"""状態を人間・担当への文章にする（次にすること・仕様シート・対応表・状態の説明）。判定は state_policy を使う。

state.py から本文を変えずに移した。"""

from . import gate as G
from .decision_sheet import other_options, recommended_option
from .util import (short)
from .state_model import AGENT_CALL, PHASE_OF_ROUTE, ROLES, ROLE_LABEL, deferred_list, deferred_ok, iter_verifications
from .state_policy import last_impl, latest_review, problems, running_agents


def next_action(ctx):
    """(フェーズ名, 次にすること, 未充足の一覧)"""
    s = ctx.state
    st = s.get("status")
    if st in ("complete", "closed"):
        return "完了", "この run は完了しています。新しい開発は new で別の run を始めてください。", []
    if st == "aborted":
        return "中断", "この run は中断されています。", []
    run = running_agents(s)
    if run:
        roles = "、".join(ROLE_LABEL.get(v["role"], v["role"]) for v in run.values())
        return "サブエージェント実行中", f"{roles} の完了を待つ（結果はエンジンが自動で記録する）。", []
    if s.get("awaiting_human"):
        return "人間の返答待ち", f"人間の返答を待つ: {s.get('question')}（返答後に resume）", []
    stage = "final" if st == "pr_ready" else "pr"
    probs = problems(ctx, stage)
    if not probs:
        if st == "pr_ready":
            return "完了判定", "complete を実行する（すべての証拠が揃っている）。", []
        return "完了判定", "complete を実行し、PR を作成する。", []
    p = probs[0]
    return PHASE_OF_ROUTE.get(p["route"], p["route"]), instruction(p, ctx), probs


def instruction(p, ctx):
    r, c = p["route"], p["code"]
    head = p["msg"] + "。"
    if r in ROLES:
        hint = AGENT_CALL.format(agent=f"aq-dlc:{r}")
        if r == "requirements" and c == "spec_missing":
            hint += " prompt にはユーザーの依頼文をそのまま含める。"
        return head + "\n→ " + hint
    if r == "human":
        if c == "state_invalid":
            return (head + "\n→ run の記録が壊れているか、エンジン以外で書き換えられています。人間に状況を報告し、"
                    "原因を確認してもらう（audit で監査ログを確認できる）。正しい状態に戻せない場合は new で run をやり直す。")
        if c == "questions_open":
            return (head + "\n→ show <質問ID> で内容を確認し、AskUserQuestion で人間に聞く（1回で最大4問）。"
                    "回答を answer <質問ID> \"<回答>\" で記録する。")
        from .approval import target_id
        if c == "spec_unpresented":
            t = target_id("SPEC", ctx.spec_hash)
            return (head + "\n→ spec --present で仕様シートを出し、要点を短く見せてから、AskUserQuestion で"
                    f"「この仕様で進めてよいか」を確認する。選択肢は「この仕様で進める（承認 {t}）」と「修正したい」の形にする"
                    f"（人間が「承認 {t}」を選ぶか書いた返答だけを承認として扱う）。")
        if c == "spec_unapproved":
            t = target_id("SPEC", ctx.spec_hash)
            return (head + f"\n→ 人間が「承認 {t}」を選んだ・書いたら spec --approve \"<人間の返答の要約>\"。"
                    "「OK」などの対象のない返答は承認として扱われないので、選択肢を示して聞き直す。修正の要望なら "
                    "answer ではなく aq-dlc:requirements に修正を依頼する（依頼内容は question で記録してもよい）。")
        if c == "config_changed":
            t = target_id("CFG", ctx.config_fp)
            return (head + "\n→ 変更前後の設定（git diff .aq-dlc/config.json、~/.aq-dlc/config.json）を確認し、ask を実行してから"
                    f"人間に変更を受け入れるか確認する（選択肢「受け入れる（承認 {t}）」「元に戻す」）。"
                    f"「承認 {t}」の返答を得たら config-accept \"<人間の返答の要約>\"、受け入れないなら人間に元に戻してもらう。")
        if c == "forged_human":
            ids = [f["id"] for f in ctx.state.get("forged_events", []) if not f.get("resolved")]
            return (head + "\n→ audit で内容を確認し、人間に状況を報告する。ask を実行してから、人間が確認したら"
                    f"「承認 {ids[0]}」の返答を得て audit --resolve {ids[0]}。取り消された承認は、人間に聞き直して取り直す。")
        if c == "memory_scope":
            from .approval import memscope_target
            t = memscope_target(ctx.state)
            return (head + "\n→ ask を実行してから AskUserQuestion で、この run の Memory の利用範囲を聞く。選択肢は"
                    f"「プロジェクト内だけ（推奨）」「Memory を使わない」「共通 Memory も使う（承認 {t}）」。"
                    "返答に合わせて memory-scope project|none|all を実行する（all は「承認 …」の返答が必要）。"
                    "人間に聞けない実行（非対話）では memory-scope project を実行する（共通 Memory は使わない）。")
        if c == "cf_insufficient":
            return (head + "\n→ cf のログ（runs/<run>/cf/）で原因（テストの起動失敗・時間切れ・ベースライン不合格など）を確認し、"
                    "ask を実行してから人間に方針を確認する（テスト環境を直す / counterfactual.mode を advisory に変える）。")
        if c == "manual_unconfirmed":
            return (head + "\n→ ask \"<確認してほしいこと（まとめて）>\" を実行してから人間に確認する。選択肢は検証ごとに"
                    "「確認できた（承認 VERIFY-<検証ID>）」「確認できないまま進める（承認 DEFER-<検証ID>）」の2つを示す。"
                    "「承認 VERIFY-…」の返答は confirm <検証ID> \"<人間の返答の要約>\"、"
                    "「承認 DEFER-…」の返答は defer <検証ID> \"<人間の返答の要約>\" で記録する。"
                    "未確認のまま進めた検証は合格ではないので、完了報告と PR に「未確認」として必ず書く。"
                    "人が確認できない検証のために仕様を変えたり、run を中断したりする前に、まずこの選択肢を示す。")
        return head + "\n→ 修正ループが上限に達した。状況を要約し、ask を実行してから人間に方針を確認する。"
    if r == "orchestrator":
        return (head + "\n→ escalation で内容を確認する。仕様の変更が必要なら人間に確認して aq-dlc:requirements、"
                "設計の変更なら aq-dlc:design に依頼する。対応したら escalation <ID> --resolve \"<対応>\"。")
    if r == "gate":
        return head + "\n→ gate を実行する。"
    if r == "counterfactual":
        return head + "\n→ cf を実行する（隔離したコピーで欠陥を埋め込み、テストが失敗するかを確かめる。計画だけなら cf --plan）。"
    if r == "memory":
        return (head + "\n→ aq-dlc:memory Skill の手順（Software Reliability Engineer の観点: 証拠の実在・適用条件・"
                "有害性）で学びを整理する。終了時の保存先（プロジェクト内だけ / 共有候補を1件ずつ承認 / 保存しない）を"
                "人間に確認し、memory-done --end project|share|none を実行する。")
    if r == "ci":
        return head + "\n→ 作業ブランチを commit・push し、ci --wait 8 で結果を確認する。"
    if r == "after_merge":
        return (head + "\n→ PR がマージされたら既定ブランチを取り込み、gate --after-merge を実行する。")
    return head


# ------------------------------------------------------------------ display helpers
def pending_by_sheet(state):
    """判断シートで一緒に決める項目: 推奨のある未回答の質問、人の確認が必要な検証。"""
    qs = [q for q in state.get("questions", []) if q.get("status") != "answered" and recommended_option(q)]
    manual = [v for _r, _p, v in iter_verifications(state) if not G.is_automated(v)
              and (v.get("manual") or v.get("after_merge")) and v.get("confirmed_by") != "human"
              and not v.get("deferred")]
    return qs, manual


def spec_sheet(state):
    lines = [f"# 仕様の確認: {state.get('title')}", ""]
    for kind, label in (("functional", "機能要件"), ("nonfunctional", "非機能要件")):
        reqs = [r for r in state["requirements"] if r.get("kind", "functional") == kind]
        if not reqs:
            continue
        lines += [f"## {label}"]
        for r in reqs:
            lines.append(f"- {r['id']}{' 【重要】' if r.get('critical') else ''} {r.get('text')}")
            for a in r.get("acceptance", []):
                lines.append(f"    - 受け入れ条件 {a['id']}: {a.get('text')}")
            for k in r.get("risks", []):
                vs = "、".join(v.get("method", "") + ("（マージ後）" if v.get("after_merge") else "")
                              + ("（人が確認）" if v.get("manual") and not G.is_automated(v) else "")
                              for v in k.get("verifications", []))
                lines.append(f"    - 想定する失敗 {k['id']}: {k.get('text')}" + (f" → 確かめ方: {vs}" if vs else ""))
        lines.append("")
    if state.get("assumptions"):
        lines += ["## こちらで決めたこと（仮定）"] + [f"- {a['id']} {a['text']}" for a in state["assumptions"]] + [""]
    if state.get("out_of_scope"):
        lines += ["## 今回は作らないもの"] + [f"- {o['id']} {o['text']}" for o in state["out_of_scope"]] + [""]
    answered = [q for q in state.get("questions", []) if q.get("status") == "answered"]
    if answered:
        lines += ["## 質問と回答"]
        for q in answered:
            lines.append(f"- {q['id']} {q['text']} → {q.get('answer')}")
        lines.append("")
    open_q, manual = pending_by_sheet(state)
    if open_q or manual:
        lines += ["## 判断シート（この仕様の承認と一緒に、1回の返答で決められます）"]
        for q in open_q:
            others = other_options(q)
            lines.append(f"- {q['id']} {q['text']} → **推奨: {recommended_option(q)}**"
                         + (f"（ほかの選択肢: {' / '.join(others)}）" if others else ""))
        for v in manual:
            how = v.get("manual") or v.get("method", "")
            lines.append(f"- {v['id']} 人の確認が必要: {v.get('method', '')}（手順: {how}）"
                         f" → 後で確認する（返答不要）／確認しないで進める（承認 DEFER-{v['id']}。合格にはならず、未確認として報告）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def trace_rows(ctx):
    s = ctx.state
    g = s.get("gate") or {}
    vres = (g.get("results") or {}).get("verifications", {})
    fresh_code = g.get("code_fp") == ctx.code_fp and g.get("checks_fp") == ctx.checks_fp
    rows = []
    for r in s["requirements"]:
        design = s["design"]["map"].get(r["id"], "")
        files = ", ".join(s["impl"]["map"].get(r["id"], []))
        nodes = [("受入", a) for a in r.get("acceptance", [])] + [("リスク", k) for k in r.get("risks", [])]
        if not nodes:
            rows.append([r["id"], "", design, files, "", "", "", ""])
        for label, n in nodes:
            vs = n.get("verifications", []) or [{}]
            for v in vs:
                res = vres.get(v.get("id"), {}) if v else {}
                if not v:
                    status = "検証なし"
                elif v.get("confirmed_by") == "human":
                    status = "人が確認済み"
                elif not G.is_automated(v) and deferred_ok(v, ctx.cfg):
                    status = "未確認（人の判断で保留）"
                elif res.get("pending_after_merge"):
                    status = "マージ後"
                elif not res:
                    status = "未実行"
                else:
                    ok = res.get("pass")
                    fresh = fresh_code and res.get("fp") == G.verification_fp(v, ctx.cfg)
                    status = ("合格" if ok else "不合格") + ("" if fresh else "（古い）")
                rows.append([r["id"], f"{label} {n['id']}", design, files,
                             f"{v.get('id', '')}" + (f"（{v.get('level')}）" if v.get("level") else ""),
                             v.get("test") or v.get("command") or ",".join(v.get("checks") or [])
                             or (v.get("manual") and "人が確認") or "",
                             status, short(g.get("stamp", ""))[:15] if g else ""])
    return rows


def first_line(s, n=60):
    s = (s or "").splitlines()[0] if s else ""
    return s if len(s) <= n else s[:n - 1] + "…"


def describe_state(ctx):
    s = ctx.state
    spec, d = s.get("spec") or {}, s["design"]
    li, rv = last_impl(s), latest_review(s)
    lines = [
        f"仕様: {short(ctx.spec_hash)}  登録 v{spec.get('version', 0)}"
        f"  承認{'済み' if spec.get('approved_hash') == ctx.spec_hash else 'なし' if not spec.get('approved_hash') else '（古い）'}",
        f"設計: {short(ctx.design_hash) if ctx.design_hash else '未作成'}"
        f"  登録{'済み' if d.get('registered_hash') and d.get('registered_hash') == ctx.design_hash else 'なし'}",
        f"実装: {('第' + str(li['n']) + '回') if li else '未着手'}  修正回数: {s.get('fix_iterations', 0)}",
        f"コード: {short(ctx.code_fp)}  チェック: {len(ctx.checks)}件",
        f"レビュー: {('#' + str(rv['n']) + ' ' + str(rv.get('verdict')) + ' 重大' + str(rv['blocking'])) if rv else '未実施'}",
    ]
    g = s.get("gate") or {}
    if g.get("modified_during_gate"):
        lines.append(f"  ! 最後の gate（{g.get('stamp')}）は、gate の実行中に変更があったため不合格: "
                     f"{g['modified_during_gate'].get('summary')}")
    vio = s.get("violations") or []
    if vio:
        lines.append(f"  ! 担当外の変更の記録: {len(vio)} 件（最新: {vio[-1]['role']} の Bash で {vio[-1]['count']} 件）")
    bad = [r for r in s.get("reviews", []) if not r.get("valid")]
    if bad and (not rv or bad[-1]["n"] > rv["n"]):
        lines.append(f"  ! 最後のレビュー#{bad[-1]['n']} は無効: {bad[-1].get('invalid_reason')}")
    dl = deferred_list(s, ctx.cfg)
    if dl:
        lines.append(f"未確認のまま進めた検証（人間の判断）: {', '.join(dl)}")
    from . import workflow as WF
    if WF.config_mode(ctx.cfg) != "full":
        lines.append(WF.describe(s, ctx.cfg))
    return lines
