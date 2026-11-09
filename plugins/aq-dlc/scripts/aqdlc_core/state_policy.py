"""状態の判定（整合性の規則・gate の結果の鮮度・完了までに足りないもの）。
ファイル・git には触れない。必要な事実（指紋・ファイルの有無・CI の対象のコミット）は Ctx から受け取る。
状態の保存（state_store）は呼ばない。副作用は呼び出し側で行う。

state.py から本文を変えずに移した。"""
import time

from . import gate as G
from . import verdict as V
from .decision_sheet import recommended_option
from .state_model import ID_RE, SCHEMA, STATUSES, deferred_ok, id_index, iter_verifications


def validate_state(state):
    """スキーマ・状態遷移・参照 ID・gate とレビューと承認の整合性を確かめる（問題の一覧を返す）。

    これはエンジンの不具合や単純な書き換えを見つけるためのもので、意図的な改ざんへの耐性を
    保証するものではない。
    """
    from .approval import approved, target_id
    errs = []
    if not isinstance(state, dict):
        return ["state が辞書ではない"]
    if state.get("schema_version") != SCHEMA:
        errs.append(f"schema_version が {SCHEMA} ではない")
    if state.get("status") not in STATUSES:
        errs.append(f"status が不正: {state.get('status')}")
    seq = state.get("seq", 0)
    if not isinstance(seq, int) or seq < 0:
        errs.append("seq が不正")
        seq = 0
    reqs = state.get("requirements")
    if not isinstance(reqs, list):
        return errs + ["requirements が一覧ではない"]
    req_ids = set()
    for r in reqs:
        if not isinstance(r, dict) or not ID_RE.match(str(r.get("id", ""))):
            errs.append(f"要求の ID が不正: {r.get('id') if isinstance(r, dict) else r}")
            continue
        req_ids.add(r["id"])
        for key in ("acceptance", "risks"):
            for n in r.get(key, []) or []:
                if not ID_RE.match(str(n.get("id", ""))):
                    errs.append(f"{r['id']} の子の ID が不正: {n.get('id')}")
                for v in n.get("verifications", []) or []:
                    if not ID_RE.match(str(v.get("id", ""))):
                        errs.append(f"{n.get('id')} の検証の ID が不正: {v.get('id')}")
                    if v.get("confirmed_by") == "human" and state.get("eaqdl"):
                        if not approved(state, f"VERIFY-{v.get('id')}"):
                            errs.append(f"{v.get('id')} が人間の確認済みだが、対象を指定した承認の記録がない")
                    if v.get("deferred") and state.get("eaqdl"):
                        if not approved(state, f"DEFER-{v.get('id')}"):
                            errs.append(f"{v.get('id')} が未確認のまま進める扱いだが、対象を指定した承認の記録がない")
    for key in ("design", "impl"):
        m = (state.get(key) or {}).get("map", {}) or {}
        dangling = [k for k in m if k not in req_ids]
        if dangling:
            errs.append(f"{key} の対応表に存在しない要求がある: {', '.join(sorted(dangling)[:5])}")
    for q in state.get("questions", []):
        if q.get("status") not in ("open", "answered"):
            errs.append(f"質問 {q.get('id')} の status が不正")
        elif q.get("status") == "answered" and not q.get("answer"):
            errs.append(f"質問 {q.get('id')} が回答済みだが回答がない")
    sp = state.get("spec") or {}
    if sp.get("approved_hash"):
        if state.get("eaqdl") and not approved(state, target_id("SPEC", sp["approved_hash"])):
            errs.append("仕様が承認済みだが、対象（SPEC-…）を指定した人間の承認の記録がない")
    g = state.get("gate")
    if g:
        res = g.get("results") or {}
        if g.get("checks_pass") and (not res.get("checks") or
                                     not all(V.check_ok(r) for r in (res.get("checks") or {}).values())):
            errs.append("gate が合格になっているが、不合格・未実行のチェックがある")
        bad = [k for part in ("checks", "verifications", "holdout") for k, r in (res.get(part) or {}).items()
               if V.inconsistent(r)]
        if bad:
            errs.append(f"gate の結果の記録が食い違っている（status と pass）: {', '.join(bad[:5])}")
        if g.get("checks_pass") and g.get("modified_during_gate"):
            errs.append("gate の実行中に変更があったのに合格になっている")
        if g.get("seq", 0) > seq:
            errs.append("gate の seq が全体の seq より大きい")
    for rv in state.get("reviews", []):
        if rv.get("passed") and (not rv.get("valid") or rv.get("verdict") != "合格" or rv.get("blocking", 0)):
            errs.append(f"レビュー#{rv.get('n')} が合格になっているが、無効・判定が合格でない・重大な指摘がある")
        if rv.get("seq", 0) > seq:
            errs.append(f"レビュー#{rv.get('n')} の seq が全体の seq より大きい")
    for i, rd in enumerate((state.get("impl") or {}).get("rounds", []), 1):
        if rd.get("n") != i:
            errs.append("実装の回の番号が連続していない")
            break
    for e in state.get("human_events", []):
        if e.get("seq", 0) > seq:
            errs.append("人間の返答の seq が全体の seq より大きい")
            break
    ms = state.get("memory_scope")
    if ms:
        if ms.get("choice") not in ("none", "project", "all"):
            errs.append(f"Memory の利用範囲が不正: {ms.get('choice')}")
        elif ms.get("choice") == "all" and ms.get("source") not in ("config", "approval"):
            errs.append("共通 Memory の利用が、設定か人間の承認なしに記録されている")
        elif ms.get("choice") == "all" and ms.get("source") == "approval" and state.get("eaqdl"):
            import hashlib
            t = target_id("MEMSCOPE", hashlib.sha1(str(state.get("run_id")).encode()).hexdigest())
            if not approved(state, t):
                errs.append("共通 Memory の利用が承認済みとされているが、対象（MEMSCOPE-…）の承認の記録がない")
    events = {e.get("seq"): e for e in state.get("human_events", [])}
    for a in state.get("approvals", []):
        if a.get("decision") not in ("approve", "reject", "revoked"):
            errs.append(f"承認の記録の決定が不正: {a.get('target')}")
        elif a.get("decision") in ("approve", "reject") and a.get("target") and \
                a.get("target") not in (a.get("excerpt") or ""):
            errs.append(f"承認の記録 {a.get('target')} の元の返答に対象 ID がない")
        ev = events.get(a.get("event_seq"))
        if ev is not None and a.get("decision") == "approve" and (ev.get("kind") == "forged" or a["target"] not in ev.get("text", "")):
            if not any(b.get("target") == a["target"] and b.get("decision") == "revoked" for b in state.get("approvals", [])):
                errs.append(f"承認の記録 {a.get('target')} が、人間の返答として扱えない記録に基づいている")
    cf = state.get("counterfactual") or {}
    for run in cf.get("runs", []) or []:
        for m in run.get("mutations", []) or []:
            if m.get("result") not in ("killed", "survived", "invalid", "timeout", "not_applicable", None):
                errs.append(f"反証テスト {m.get('id')} の結果が不正: {m.get('result')}")
    return errs


def gate_status(ctx):
    """(鮮度, 合否)。鮮度: 今のコード・チェック定義・各検証条件で実行された結果か。"""
    s = ctx.state
    g = s.get("gate")
    if not g:
        return False, False
    vres = (g.get("results") or {}).get("verifications", {})
    fresh = g.get("code_fp") == ctx.code_fp and g.get("checks_fp") == ctx.checks_fp
    passing = bool(g.get("checks_pass"))
    for _r, _p, v in iter_verifications(s):
        r = vres.get(v["id"])
        if r is None or r.get("fp") != G.verification_fp(v, ctx.cfg):
            fresh = False
            continue
        if v.get("after_merge") or (v.get("manual") and not G.is_automated(v)):
            continue
        if not r.get("pass"):
            passing = False
    return fresh, passing


# ------------------------------------------------------------------ problems / next
def P(code, msg, route):
    return {"code": code, "msg": msg, "route": route}


def open_violations(state):
    return [v for v in state.get("violations", []) if not v.get("resolved")]


def review_passed(rv):
    """レビューが「合格」で、重大な指摘が0件で、形式が正しいときだけ True（読み取れない・矛盾は不合格）。"""
    if not rv or not rv.get("valid"):
        return False
    if "passed" in rv:
        return bool(rv["passed"])
    return rv.get("verdict") == "合格" and rv.get("blocking", 0) == 0  # 旧形式の記録


def current_review(state):
    """最新のレビュー（有効なものに限る）。最新が無効なら、それより前の合格は使わない（安全側）。"""
    rs = state.get("reviews", [])
    return rs[-1] if rs and rs[-1].get("valid") else None


def latest_review(state):
    rv = [r for r in state.get("reviews", []) if r.get("valid")]
    return rv[-1] if rv else None


def last_impl(state):
    rounds = state["impl"].get("rounds", [])
    return rounds[-1] if rounds else None


def graph_issues(state):
    out = []
    for r in state["requirements"]:
        if not r.get("acceptance"):
            out.append(f"{r['id']} に受け入れ条件がない")
        for a in r.get("acceptance", []):
            if not a.get("verifications"):
                out.append(f"{a['id']} に検証がない")
        if r.get("critical") and not any(k.get("verifications") for k in r.get("risks", [])):
            out.append(f"{r['id']}（重要）に検証付きのリスクがない")
        for k in r.get("risks", []):
            if not k.get("verifications"):
                out.append(f"{k['id']} に検証がない")
    return out


def problems(ctx, stage="pr"):
    """未充足の条件を、対応すべき順に返す。stage: pr = PR前（マージ後の検証を除く） / final = すべて"""
    out = _problems(ctx, stage)
    s = ctx.state
    if s.get("eaqdl") and not s.get("memory_scope") and s.get("status") in ("in_progress", "pr_ready") \
            and not (out and out[0]["code"] == "state_invalid"):
        # run の開始時に選ぶ。ほかの未充足を隠さないように先頭に加える
        out.insert(0, P("memory_scope", "Memory の利用範囲（使わない / プロジェクト内だけ / 共通も使う）が未選択", "human"))
    return out


def _problems(ctx, stage):
    s, cfg = ctx.state, ctx.cfg
    auto = cfg["automation"]
    out = []
    bad = validate_state(s)
    if bad:
        out.append(P("state_invalid", "state.json の整合性が取れていない: " + "、".join(bad[:3]), "human"))
        return out
    forged = [f["id"] for f in s.get("forged_events", []) if not f.get("resolved")]
    if forged:
        out.append(P("forged_human", f"Bash の実行中に記録された「人間の返答」（偽装の疑い）がある: {', '.join(forged)}",
                     "human"))
        return out
    reg = s.get("config_fp_registered")
    if not reg and s.get("status") in ("in_progress", "pr_ready"):
        out.append(P("config_changed", "この run には設定の指紋が登録されていない（v0.5.1 より前に始まった run）。"
                     "現在の設定を確認して受け入れる必要がある", "human"))
        return out
    if reg and reg != ctx.config_fp:
        out.append(P("config_changed", "設定（.aq-dlc/config.json または ~/.aq-dlc/config.json）がエンジンのコマンド以外で"
                     "変更された（指紋の対象・自動化の境界・チェックが変わった可能性）", "human"))
        return out
    _idx, dups = id_index(s)
    if dups:
        out.append(P("dup_ids", f"IDが重複している: {', '.join(dups)}（結果が上書きされるため判定できない）",
                     "requirements"))
    spec = s.get("spec") or {}
    if not s["requirements"] or not spec.get("registered_hash"):
        out.append(P("spec_missing", "仕様が未作成（aq-dlc:requirements が要求・受け入れ条件・リスクを整理する）",
                     "requirements"))
        return out
    gi = graph_issues(s)
    if gi:
        out.append(P("graph_incomplete", "Evidence Graph が不完全: " + "、".join(gi[:5]), "requirements"))
    if spec.get("registered_hash") != ctx.spec_hash:
        out.append(P("spec_unregistered", "仕様が要求整理の完了後に変更された（aq-dlc:requirements で整理し直す）",
                     "requirements"))
    # 推奨の選択肢がある質問は、仕様の承認（判断シート）で一緒に決められるので、提示を止めない
    openq = [q["id"] for q in s["questions"] if q.get("status") != "answered" and not recommended_option(q)]
    if openq:
        out.append(P("questions_open", f"未回答の質問（推奨の選択肢なし）: {', '.join(openq)}", "human"))
    unapplied = [q["id"] for q in s["questions"]
                 if q.get("status") == "answered" and q.get("answered_seq", 0) > spec.get("registered_seq", 0)]
    if unapplied and not openq:
        out.append(P("answers_unapplied", f"人間の回答（{', '.join(unapplied)}）が仕様に未反映", "requirements"))
    if auto.get("spec_review", True) and not openq and not unapplied:
        if spec.get("approved_hash") != ctx.spec_hash:
            if spec.get("presented_hash") != ctx.spec_hash:
                out.append(P("spec_unpresented", "仕様を人間に提示していない", "human"))
            else:
                out.append(P("spec_unapproved", "仕様が人間に未承認", "human"))
    if out and out[-1]["route"] in ("human", "requirements"):
        return out
    esc = [e["id"] for e in s["escalations"] if e.get("status") != "resolved"]
    if esc:
        out.append(P("escalation", f"未対応の差し戻し: {', '.join(esc)}", "orchestrator"))
        return out

    from . import workflow as WF
    wf = WF.effective(s, cfg)
    d = s["design"]
    if not wf["stages"]["design"]:
        # 工程の選択で設計担当を使わない（実装担当が構成を決め、チェックを登録する）
        if d.get("registered_hash") is None and not ctx.checks and last_impl(s):
            out.append(P("no_checks", "Quality Gate のチェックが未登録（この工程では実装担当が add-check で登録する）",
                         "implementer"))
            return out
    elif not d.get("registered_hash"):
        out.append(P("design_missing", "設計が未作成", "design"))
    elif d.get("registered_hash") != ctx.design_hash:
        out.append(P("design_changed", "設計書が登録後に変更された（aq-dlc:design で登録し直す）", "design"))
    elif d.get("spec_hash") != ctx.spec_hash:
        out.append(P("design_stale", "承認済み仕様の変更後に設計が更新されていない", "design"))
    else:
        unmapped = [r["id"] for r in s["requirements"] if r["id"] not in d.get("map", {})]
        if unmapped:
            out.append(P("design_unmapped", f"設計に対応付いていない要求: {', '.join(unmapped)}", "design"))
        if not ctx.checks:
            out.append(P("no_checks", "Quality Gate のチェックが未登録", "design"))
    if out:
        return out

    max_fix = int(auto.get("max_fix_iterations", 8))

    def fix_route(route):
        if route == "implementer" and s.get("fix_iterations", 0) >= max_fix:
            return "human"
        return route

    li = last_impl(s)
    if not li:
        out.append(P("impl_missing", "実装が未着手", "implementer"))
        if open_violations(s):
            out[-1]["msg"] += "（実装の前に担当外の変更の記録がある。実装担当が確認して取り込む）"
        return out
    if li.get("design_hash") != ctx.design_hash or li.get("spec_hash") != ctx.spec_hash:
        out.append(P("impl_stale", "仕様・設計の変更後に実装が更新されていない", "implementer"))
        return out
    if li.get("code_fp") and li["code_fp"] != ctx.code_fp:
        diff = G.describe_diff(li.get("snapshot"), ctx.snap) if li.get("snapshot") else ""
        out.append(P("code_unowned", "実装担当の登録後に、実装担当以外の操作でコードが変わった"
                     + (f"（{diff}）" if diff else "") + "。実装担当に内容の確認と取り込みを依頼する",
                     fix_route("implementer")))
        return out
    openv = open_violations(s)
    if openv:
        files = sorted({f for v in openv for f in v.get("files", [])})
        out.append(P("violation_open", f"未対応の担当外の変更の記録が {len(openv)} 件ある（{', '.join(files[:3]) or '対象不明'}）。"
                     "実装担当に確認と取り込みを依頼する", fix_route("implementer")))
        return out
    untraced = []
    for r in s["requirements"]:
        files = s["impl"]["map"].get(r["id"]) or []
        missing = [f for f in files if not ctx.file_exists(f)]
        if not files:
            untraced.append(f"{r['id']}（実装ファイル未登録）")
        elif missing:
            untraced.append(f"{r['id']}（存在しないファイル: {', '.join(missing[:2])}）")
    if untraced:
        out.append(P("impl_untraced", "要求と実装の対応が不足: " + "、".join(untraced[:5]), "implementer"))
    unbound = [v["id"] for _r, _p, v in iter_verifications(s) if not G.is_automated(v) and not v.get("manual")]
    if unbound:
        out.append(P("verif_unbound", f"実行方法が未設定の検証: {', '.join(unbound)}（bind で設定）", "implementer"))
    if out:
        return out

    rv = current_review(s) if wf["stages"]["review"] else None
    if rv and not review_passed(rv):
        cats = {f.get("category") for f in rv.get("findings", []) if f.get("severity") == "重大"} or \
            {f.get("category") for f in rv.get("findings", [])}
        if "仕様" in cats and spec.get("registered_seq", 0) < rv["seq"]:
            out.append(P("review_fix", f"レビュー#{rv['n']} の重大な指摘（仕様）が未対応", "requirements"))
        elif "設計" in cats and d.get("registered_seq", 0) < rv["seq"]:
            out.append(P("review_fix", f"レビュー#{rv['n']} の重大な指摘（設計）が未対応", "design"))
        elif li["seq"] < rv["seq"]:
            acts = [f.get("action_type") for f in rv.get("findings", []) if f.get("severity") == "重大"]
            split = ""
            if any(acts):  # 対応区分の記録がある（新しい形式の）レビューだけ、内訳を出す
                split = (f"（実装の修正 {acts.count('fix_required')} 件・検証の追加 {acts.count('missing_evidence')} 件）")
            out.append(P("review_fix", f"レビュー#{rv['n']} が「{rv.get('verdict')}」（重大 {rv['blocking']} 件・"
                         f"軽微 {rv.get('minor', 0)} 件）{split}。指摘が未対応",
                         fix_route("implementer")))
        if out:
            return out

    g = s.get("gate")
    if not g:
        out.append(P("gate_missing", "Quality Gate が未実行", "gate"))
        return out
    stale = []
    diff = G.describe_diff(g.get("snapshot"), ctx.snap)
    if g.get("code_fp") != ctx.code_fp:
        stale.append("コード（" + (diff or "内容が変化") + "）")
    if g.get("checks_fp") != ctx.checks_fp:
        stale.append("チェックの定義")
    if g.get("config_fp") and g.get("config_fp") != ctx.config_fp:
        stale.append("設定")
    from . import holdout as HO
    if HO.enabled(cfg) and HO.items(s) and g.get("holdout_fp") != ctx.holdout_fp():
        stale.append("隠し受け入れテスト（holdout）")
    vres = (g.get("results") or {}).get("verifications", {})
    changed_v, new_v = [], []
    for _r, _p, v in iter_verifications(s):
        r = vres.get(v["id"])
        if r is None:
            new_v.append(v["id"])
        elif r.get("fp") != G.verification_fp(v, cfg):
            changed_v.append(v["id"])
    if changed_v:
        stale.append(f"検証条件（{', '.join(changed_v[:5])}）")
    if new_v:
        stale.append(f"結果のない検証（{', '.join(new_v[:5])}）")
    if stale:
        out.append(P("gate_stale", "gate 後に変わったものがある: " + "、".join(stale), "gate"))
        return out
    md = g.get("modified_during_gate")
    if md:
        out.append(P("gate_modified", f"gate の実行中に変更があった（{md.get('summary')}）。この gate の結果は使えない",
                     "gate" if li["seq"] > g["seq"] else fix_route("implementer")))
        return out
    fl = V.failures(g.get("results"))
    failed = [n for n, _r in fl["checks"]]
    vfail = []
    for _r, _p, v in iter_verifications(s):
        r = vres.get(v["id"], {})
        if v.get("after_merge") or (not G.is_automated(v) and v.get("manual")):
            continue
        if not V.passed(r):
            vfail.append(v["id"])
    hfail, qfail = fl["holdout"], fl["quality"]
    if failed or vfail or hfail or qfail:
        what = "、".join(([f"チェック {', '.join(failed)}"] if failed else []) +
                        ([f"検証 {', '.join(vfail[:6])}"] if vfail else []) +
                        ([f"隠しテスト {', '.join(hfail[:6])}"] if hfail else []) +
                        [f"{label}（{V.reason_of(r)}）" for _k, label, r in qfail])
        route = "gate" if li["seq"] > g["seq"] else fix_route("implementer")
        out.append(P("gate_fail", f"gate が不合格: {what}", route))
        return out

    from . import counterfactual as CF
    cc = CF.cf_config(cfg)
    if cc["mode"] != "off":
        crec = CF.current(s)
        if not crec:
            out.append(P("cf_missing", "反証テスト（Counterfactual Gate）が未実行", "counterfactual"))
            return out
        st = ctx.cf_stale_reasons(crec)
        if st:
            out.append(P("cf_stale", "反証テストの後に変わったもの: " + "、".join(st), "counterfactual"))
            return out
        if cc["mode"] == "enforce":
            gaps = CF.critical_gaps(s, crec)
            surv = [g for g in gaps if g[2]]
            if surv:
                out.append(P("cf_survived", "重要な要求の検証が、埋め込んだ欠陥を見逃した: "
                             + "、".join(f"{v}（{why}）" for v, why, _m in surv[:4])
                             + "。テストを強化する", fix_route("implementer")))
                return out
            if gaps:
                out.append(P("cf_insufficient", "重要な要求の検証で、反証テストが成立しなかった: "
                             + "、".join(f"{v}（{why}）" for v, why, _m in gaps[:4]), "human"))
                return out

    if not wf["stages"]["review"]:
        rv = None  # 工程の選択で独立レビューを使わない（省略として記録し、レビュー済みとは扱わない）
    elif not rv:
        bad = [r for r in s.get("reviews", []) if not r.get("valid")]
        why = f"（前回のレビューは無効: {bad[-1].get('invalid_reason')}）" if bad else ""
        out.append(P("review_missing", "独立レビューが未実施" + why, "reviewer"))
        return out
    if rv:
        rs = []
        if rv.get("code_fp") != ctx.code_fp:
            rs.append("コード")
        if rv.get("graph_fp") != ctx.graph_fp:
            rs.append("検証条件")
        if rv.get("checks_fp") != ctx.checks_fp:
            rs.append("チェックの定義")
        if rv.get("config_fp") and rv.get("config_fp") != ctx.config_fp:
            rs.append("設定")
        if rv.get("spec_hash") != ctx.spec_hash:
            rs.append("仕様")
        if rv.get("design_hash") != ctx.design_hash:
            rs.append("設計")
        if rv.get("role_fp"):
            if ctx.reviewer_role_fp() != rv["role_fp"]:
                rs.append("レビューの役割の定義（Role Profile）")
        if cc["mode"] != "off" and rv.get("cf_fp") != CF.record_fp(s):
            rs.append("反証テストの結果")
        if rs:
            out.append(P("review_stale", f"レビュー#{rv['n']} の後に {'・'.join(rs)} が変わった（再レビューが必要）",
                         "reviewer"))
            return out
        if not review_passed(rv):
            out.append(P("review_blocking", f"レビュー#{rv['n']} が合格ではない（判定「{rv.get('verdict')}」・"
                         f"重大 {rv['blocking']} 件）。修正して再レビューが必要",
                         fix_route("implementer")))
            return out

    manual_pre = [v["id"] for _r, _p, v in iter_verifications(s)
                  if v.get("manual") and not G.is_automated(v) and not v.get("after_merge")
                  and v.get("confirmed_by") != "human" and not deferred_ok(v, ctx.cfg)]
    if manual_pre:
        out.append(P("manual_unconfirmed", f"人間の確認待ちの検証: {', '.join(manual_pre)}", "human"))
    if not s.get("memory_done"):
        out.append(P("memory", "Memory 整理が未実施", "memory"))
    if out:
        return out

    if stage == "pr":
        head = ctx.ci_head()
        if head is not None:
            ci = s.get("ci") or {}
            if ci.get("sha") != head:
                out.append(P("ci", "最新のコミットに対する CI が未確認", "ci"))
            elif ci.get("result") != "success":
                out.append(P("ci", f"CI が{'失敗' if ci.get('result') == 'failure' else '未完了'}",
                             "ci" if ci.get("result") != "failure" else fix_route("implementer")))
        return out

    # final
    am = []
    for _r, _p, v in iter_verifications(s):
        if not v.get("after_merge"):
            continue
        if v.get("confirmed_by") == "human":
            continue
        if not G.is_automated(v) and deferred_ok(v, ctx.cfg):
            continue
        r = vres.get(v["id"], {})
        if not G.is_automated(v):
            am.append((v["id"], "human"))
        elif not s.get("after_merge_gate") or r.get("pending_after_merge"):
            am.append((v["id"], "after_merge"))
        elif not r.get("pass"):
            am.append((v["id"], "after_merge_fail"))
    if any(k == "after_merge" for _i, k in am):
        out.append(P("after_merge", "マージ後の検証が未実行: " + ", ".join(i for i, k in am if k == "after_merge"),
                     "after_merge"))
    elif any(k == "after_merge_fail" for _i, k in am):
        out.append(P("after_merge_fail", "マージ後の検証が不合格: "
                     + ", ".join(i for i, k in am if k == "after_merge_fail"), fix_route("implementer")))
    if any(k == "human" for _i, k in am):
        out.append(P("manual_unconfirmed", "マージ後に人間の確認が必要: "
                     + ", ".join(i for i, k in am if k == "human"), "human"))
    return out


def running_agents(state, now=None):
    now = time.time() if now is None else now
    return {k: v for k, v in state["agents"].get("running", {}).items()
            if now - v.get("epoch", now) < 3 * 3600}
