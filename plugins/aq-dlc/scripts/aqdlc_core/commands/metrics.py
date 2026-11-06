"""metrics コマンドと、run の計測の集計（report・complete からも使う）。

cli.py から本文を変えずに移した。"""
import datetime as _dt
import json
import os

from .. import agents as A
from ..project import (config, resolve_project)
from ..state import (ROLE_LABEL, Ctx)
from ..util import (load_json, now_iso, pop_flag)
from . import open_state


def _secs(a, b):
    try:
        return (_dt.datetime.fromisoformat(b) - _dt.datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return 0


TOKEN_KEYS = ("input", "output", "cache_read", "cache_create")


METRICS_SCHEMA = 2


def _add_tokens(acc, t):
    """トークン使用量を足す。t が None（取得できず）なら acc を変えない。"""
    if not t:
        return acc
    acc = acc or {k: 0 for k in TOKEN_KEYS + ("api_calls",)}
    acc.setdefault("models", [])
    for k in TOKEN_KEYS:
        acc[k] = acc.get(k, 0) + int(t.get(k) or 0)
    # 旧版で記録した使用量には API 呼び出し回数がない。その場合は回数を「不明」にする（0 と混同しない）
    acc["api_calls"] = None if acc.get("api_calls") is None or t.get("api_calls") is None \
        else acc["api_calls"] + int(t["api_calls"])
    acc["models"] = sorted(set(acc["models"]) | set(t.get("models") or []))
    return acc


def _engine_version():
    # このファイルは aqdlc_core/commands/ にある。プラグインのフォルダは aqdlc_core の2つ上（scripts の上）
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    here = os.path.dirname(os.path.dirname(pkg))
    return (load_json(os.path.join(here, ".claude-plugin", "plugin.json"), {}) or {}).get("version")


STAGE_OF_ROLE = (("requirements", "requirements"), ("design", "design"), ("implementation", "implementer"),
                 ("review", "reviewer"))


def collect_metrics(s, project=None):
    """運用の指標。取得できない値は None（0 と区別する）。料金には換算しない（料金表は持たない）。"""
    hist = s["agents"].get("history", [])
    roles = {}
    for h in hist:
        r = roles.setdefault(h["role"], {"runs": 0, "sec": 0.0, "ok": 0, "stop_blocks": 0, "tokens": None,
                                         "calls_with_usage": 0, "calls_without_usage": 0})
        r["runs"] += 1
        r["ok"] += 1 if h.get("ok") else 0
        r["sec"] += h.get("sec") or _secs(h.get("start"), h.get("end"))
        r["stop_blocks"] += h.get("stop_blocks", 0)
        if h.get("tokens"):
            r["tokens"] = _add_tokens(r["tokens"], h["tokens"])
            r["calls_with_usage"] += 1
        else:
            r["calls_without_usage"] += 1
    gh = s.get("gate_history", [])
    reviews = s.get("reviews", [])
    end = s.get("closed_at") or now_iso()
    orch = A.transcript_usage(s.get("session_transcript"), since=s.get("created"), until=s.get("closed_at"))
    orch_session = A.transcript_usage(s.get("session_transcript"))
    cf = _cf_metrics(s)
    stages = []
    for stage, role in STAGE_OF_ROLE:
        r = roles.get(role)
        stages.append({"stage": stage, "agent": role, "calls": r["runs"] if r else 0,
                       "elapsed_seconds": round(r["sec"], 1) if r else 0,
                       "tokens": r["tokens"] if r else None,
                       "usage_complete": bool(r) and r["calls_without_usage"] == 0})
    stages.append({"stage": "gate", "agent": None, "calls": len(gh),
                   "elapsed_seconds": round(sum(g.get("sec") or 0 for g in gh), 1), "tokens": None,
                   "usage_complete": True})
    cf_runs = (s.get("counterfactual") or {}).get("runs", [])
    cf_secs = [x.get("sec") for x in cf_runs if isinstance(x, dict)]
    stages.append({"stage": "counterfactual", "agent": None, "calls": cf["runs"],
                   "elapsed_seconds": (round(sum(cf_secs), 1) if cf_secs and all(v is not None for v in cf_secs)
                                       else (0 if not cf_runs else None)),
                   "tokens": None, "usage_complete": True})
    stages.append({"stage": "memory", "agent": None, "calls": None, "elapsed_seconds": None, "tokens": None,
                   "usage_complete": True})
    total = None
    for r in roles.values():
        total = _add_tokens(total, r["tokens"])
    total = _add_tokens(total, orch)
    complete = all(r["calls_without_usage"] == 0 for r in roles.values()) and orch is not None
    valid_reviews = [r for r in reviews if r.get("valid")]
    return {
        "schema": METRICS_SCHEMA,
        "engine_version": _engine_version(),
        "token_source": "claude_code_transcript",
        "token_note": "Claude Code の会話記録にある usage の合計。料金ではない。cache_read と cache_create は課金区分が別",
        "run_sec": round(_secs(s.get("created"), end)),
        "roles": roles,
        "stages": stages,
        "gate": {"runs": len(gh), "failed": sum(1 for g in gh if not g.get("pass")),
                 "modified_during": sum(1 for g in gh if g.get("modified")),
                 "sec": round(sum(g.get("sec") or 0 for g in gh), 1)},
        "reviews": {"runs": len(reviews), "passed": sum(1 for r in reviews if r.get("passed")),
                    "needs_fix": sum(1 for r in reviews if r.get("valid") and not r.get("passed")),
                    "invalid": sum(1 for r in reviews if not r.get("valid"))},
        "rework": {"gate_failures": sum(1 for g in gh if not g.get("pass")),
                   "review_retries": max(0, len(valid_reviews) - 1),
                   "fix_iterations": s.get("fix_iterations", 0),
                   "agent_reruns": {k: max(0, v["runs"] - 1) for k, v in roles.items()}},
        "fix_iterations": s.get("fix_iterations", 0),
        "human": {"replies": len(s.get("human_events", [])), "asks": s.get("asks_total", 0)},
        "stop_blocks_total": s.get("stop_blocks_total", 0),
        "violations": len(s.get("violations", [])),
        "orchestrator_tokens": orch,
        "orchestrator_tokens_session": orch_session,
        "orchestrator_tokens_note": "orchestrator_tokens は run の期間（created〜closed_at）だけ。"
                                    "_session は会話全体（init・Skill の読み込みや、同じ会話の別の run を含みうる）",
        "tokens_total": total,
        "tokens_complete": complete,
        "models": (total or {}).get("models") or [],
        "quality": _quality_metrics(s, project),
        "role_profiles": _role_metrics(s),
        "counterfactual": cf,
        "memory": _memory_metrics(s),
        "approvals": {"approve": sum(1 for a in s.get("approvals", []) if a.get("decision") == "approve"),
                      "reject": sum(1 for a in s.get("approvals", []) if a.get("decision") == "reject")},
        "conditions": {"memory_scope": (s.get("memory_scope") or {}).get("choice"),
                       "counterfactual_mode": (((s.get("counterfactual") or {}).get("latest") or {}).get("mode")),
                       "roles_enabled": bool((s.get("roles") or {}).get("history")),
                       "workflow": _workflow_label(s, project),
                       "engine": "E-AQDL" if s.get("eaqdl") else "AQ-DLC v0.5"},
    }


def _workflow_label(s, project):
    from .. import workflow as WF
    try:
        cfg = config(project) if project else config()
        if WF.config_mode(cfg) == "full":
            return "full"
        return WF.effective(s, cfg)["mode"]
    except Exception:  # noqa: BLE001
        return None


def _quality_metrics(s, project):
    """エンジンが数えられる品質の材料。外部の受け入れ評価・重大欠陥数はエンジンでは算出しない（外部評価器が付ける）。"""
    out = {"unverified": None, "deferred": None, "evidence_counts": None,
           "external_acceptance_pass_rate": None, "critical_defects": None,
           "note": "external_acceptance_pass_rate・critical_defects は外部の評価器で付ける（エンジンは算出しない）"}
    if not project:
        return out
    try:
        from .. import evidence as EV
        from ..state import deferred_list
        ctx = Ctx(project, s)
        rows = EV.compute(ctx)
        counts = {}
        for x in rows:
            counts[x["state"]] = counts.get(x["state"], 0) + 1
        out.update(evidence_counts=counts,
                   unverified=sum(1 for x in rows if x["state"] in ("insufficient", "invalidated")),
                   deferred=len(deferred_list(s, ctx.cfg)))
    except Exception:  # noqa: BLE001 — 指標の材料が読めなくても、他の指標は出す
        pass
    return out


def _role_metrics(s):
    hist = (s.get("roles") or {}).get("history", [])
    domains = {}
    for h in hist:
        for d in h.get("domain", []):
            domains[d["id"]] = domains.get(d["id"], 0) + 1
    return {"applications": len(hist), "by_trigger": {t: sum(1 for h in hist if h.get("trigger") == t)
                                                      for t in ("start", "transition", "sendback")},
            "domains": domains}


def _cf_metrics(s):
    from .. import counterfactual as CF
    cf = s.get("counterfactual") or {}
    rec = cf.get("latest")
    out = {"runs": len(cf.get("runs", [])), "latest": None,
           "score_definition": "killed ÷（killed＋survived）。invalid・timeout・対象外・等価は母集団から除外し、件数を別に示す"}
    if rec:
        sm = CF.summarize(rec)
        out["latest"] = dict(sm, stamp=rec.get("stamp"), status=rec.get("status"))
    return out


def _memory_metrics(s):
    mem = s.get("memory") or {}
    used = mem.get("used", {})
    res = {}
    for v in used.values():
        res[v.get("result")] = res.get(v.get("result"), 0) + 1
    share = mem.get("share", {})
    return {"scope": (s.get("memory_scope") or {}).get("choice"), "loaded": len(mem.get("loaded", {})),
            "results": res, "shared": sum(1 for v in share.values() if v.get("status") == "shared"),
            "share_rejected": sum(1 for v in share.values() if v.get("status") == "rejected"),
            "end": (mem.get("report") or {}).get("end")}


def cmd_metrics(args):
    as_json = pop_flag(args, "--json")
    all_runs = pop_flag(args, "--runs")
    if all_runs:
        return _metrics_runs(args, as_json)
    project, s, _ = open_state(args)
    m = collect_metrics(s, project)
    if as_json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return

    def tok(t):
        if not t:
            return "取得できず"
        return (f"入力 {t['input']:,} / 出力 {t['output']:,} / キャッシュ読込 {t['cache_read']:,} / "
                f"キャッシュ書込 {t['cache_create']:,}"
                + (f"（API {t['api_calls']} 回）" if t.get("api_calls") else "（API 回数は不明）"))
    print(f"run 全体の経過時間: {m['run_sec'] // 60} 分 {m['run_sec'] % 60} 秒")
    print("| 担当 | 実行回数 | 再実行 | 所要時間 | 終了の差し戻し | トークン |\n|---|---|---|---|---|---|")
    for role, r in m["roles"].items():
        print(f"| {ROLE_LABEL.get(role, role)} | {r['runs']} | {max(0, r['runs'] - 1)} | {round(r['sec'])} 秒 | "
              f"{r['stop_blocks']} | {tok(r['tokens'])} |")
    g = m["gate"]
    print(f"Quality Gate: {g['runs']} 回（不合格 {g['failed']} 回、うち実行中の変更 {g['modified_during']} 回、計 {g['sec']} 秒）")
    rv = m["reviews"]
    print(f"独立レビュー: {rv['runs']} 回（合格 {rv['passed']}・要修正 {rv['needs_fix']}・無効 {rv['invalid']}）"
          f"  修正ループ: {m['fix_iterations']} 回")
    print(f"人間の介入: 返答 {m['human']['replies']} 回・確認依頼 {m['human']['asks']} 回  "
          f"Stop hook による継続: {m['stop_blocks_total']} 回  担当外の変更: {m['violations']} 件")
    print(f"統括のトークン（run の期間）: {tok(m['orchestrator_tokens'])}")
    print(f"統括のトークン（会話全体）: {tok(m['orchestrator_tokens_session'])}")
    print(f"合計のトークン: {tok(m['tokens_total'])}"
          + ("" if m["tokens_complete"] else "（一部の担当・統括の使用量が取得できず、合計は下限）")
          + f"  モデル: {', '.join(m['models']) or '不明'}")
    print("※ 会話記録の usage の合計で、料金ではありません（キャッシュ読込・書込は課金区分が別）。")
    q = m["quality"]
    print(f"未検証（証拠不足・失効）: {q['unverified'] if q['unverified'] is not None else '算出できず'} 件"
          f"（うち人の判断で未確認 {q['deferred'] if q['deferred'] is not None else '-'} 件）。"
          "外部の受け入れ評価・重大欠陥数は外部の評価器で付けます。")
    rp = m["role_profiles"]
    print(f"Role Profile の適用: {rp['applications']} 回（開始 {rp['by_trigger']['start']}・遷移 {rp['by_trigger']['transition']}"
          f"・差し戻し {rp['by_trigger']['sendback']}）追加の専門性: "
          + ("、".join(f"{k} {v}" for k, v in rp["domains"].items()) or "なし"))
    cf = m["counterfactual"]
    if cf["latest"]:
        c = cf["latest"]["counts"]
        print(f"反証テスト: {cf['runs']} 回。最新 killed {c['killed']}・survived {c['survived']}・invalid {c['invalid']}・"
              f"timeout {c['timeout']}・対象外 {c['not_applicable']}・等価 {cf['latest']['equivalent']}、"
              f"検出率 {cf['latest']['score']}（{cf['score_definition']}）")
    else:
        print("反証テスト: 未実行")
    mm = m["memory"]
    print(f"Memory: 利用範囲 {mm['scope']}・読み込み {mm['loaded']} 件・結果 {mm['results'] or 'なし'}・"
          f"共有 {mm['shared']} 件（却下 {mm['share_rejected']}）・終了時 {mm['end']}")


def _metrics_runs(args, as_json):
    """metrics --runs — このプロジェクトの run を条件ごとに並べる（比較実験の材料。統計的な結論は出さない）"""
    from ..state import runs_dir, migrate
    project = resolve_project(args)
    rows = []
    d = runs_dir(project)
    for rid in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not os.path.isfile(os.path.join(d, rid, "state.json")):
            continue
        st = load_json(os.path.join(d, rid, "state.json"), None)
        if not isinstance(st, dict):
            continue
        st = migrate(st)
        m = st.get("metrics")
        if not m or m.get("schema", 1) < METRICS_SCHEMA:
            m = collect_metrics(st)
        tt = m.get("tokens_total") or {}
        rows.append({"run": rid, "status": st.get("status"), **m["conditions"], "gate_failed": m["gate"]["failed"],
                     "models": ",".join(m.get("models") or []) or None,
                     "tokens_input_total": (tt.get("input", 0) + tt.get("cache_read", 0) + tt.get("cache_create", 0))
                     if tt else None,
                     "tokens_output": tt.get("output") if tt else None, "tokens_complete": m.get("tokens_complete"),
                     "fix_iterations": m["fix_iterations"], "reviews_needs_fix": m["reviews"]["needs_fix"],
                     "cf_score": ((m["counterfactual"].get("latest") or {}).get("score")),
                     "human_asks": m["human"]["asks"], "run_sec": m["run_sec"]})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print("| run | 状態 | エンジン | 工程 | モデル | Memory | 反証 | gate 不合格 | 修正ループ | レビュー要修正 | 検出率 | 確認依頼 | 秒 "
          "| 入力トークン（キャッシュ含む） | 出力トークン |")
    print("|" + "---|" * 15)
    for r in rows:
        tin = "取得できず" if r["tokens_input_total"] is None else f"{r['tokens_input_total']:,}" + \
            ("" if r["tokens_complete"] else "（下限）")
        tout = "取得できず" if r["tokens_output"] is None else f"{r['tokens_output']:,}"
        print(f"| {r['run']} | {r['status']} | {r['engine']} | {r.get('workflow') or 'full'} | {r['models'] or '不明'} | "
              f"{r['memory_scope']} | {r['counterfactual_mode']} | "
              f"{r['gate_failed']} | {r['fix_iterations']} | {r['reviews_needs_fix']} | {r['cf_score']} | "
              f"{r['human_asks']} | {r['run_sec']} | {tin} | {tout} |")
    print(f"run 数 {len(rows)}。課題・受け入れ条件・評価基準・モデルが揃っていない run どうしの差は、改善の根拠にならない。"
          "少ない試行の差を統計的な改善と断定しない。")
