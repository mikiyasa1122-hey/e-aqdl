"""欠陥検出ベンチマークの集計。run_bench.py report から呼ぶ。

数え方（PROTOCOL.md と同じ）:
- TP: 欠陥ケースで、埋め込んだ欠陥を指摘した（判定役の defect_detected が yes。partial は手で確かめて yes / no にする）
- FN: 欠陥ケースで、埋め込んだ欠陥を指摘しなかった
- FP: 欠陥でないものを欠陥と指摘した数（false_positive に分類された指摘。欠陥ケース・欠陥なしケースの両方で数える）
- real_other（埋め込んだもの以外の実在する欠陥）・test_gap（テストの不足だけの指摘）・style は、TP にも FP にも数えず別に出す
- ambiguous（判定役が決められない）は、adjudication.json で手で決める。決めていないものは数えず、件数を出す
- 測っていない項目は not_measured（0 や成功にしない）
"""
import json
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cases as CS  # noqa: E402

NM = "not_measured"


def load(out):
    adj_path = os.path.join(out, "adjudication.json")
    adj = json.load(open(adj_path, encoding="utf-8")) if os.path.exists(adj_path) else {}
    runs = []
    for f in sorted(os.listdir(os.path.join(out, "reviews"))):
        rev = json.load(open(os.path.join(out, "reviews", f), encoding="utf-8"))
        jp = os.path.join(out, "judged", f)
        jd = json.load(open(jp, encoding="utf-8")) if os.path.exists(jp) else None
        key = f"{rev['case']}#{rev['rep']}"
        runs.append({"key": key, "rev": rev, "judge": jd, "adj": adj.get(key, {})})
    return runs


def review_header(text):
    text = text or ""
    m = re.search(r"判定:\s*(合格|要修正)", text)
    hi = re.search(r"重大な指摘:\s*(\d+)", text)
    lo = re.search(r"軽微な指摘:\s*(\d+)", text)
    return (m.group(1) if m else None, int(hi.group(1)) if hi else None, int(lo.group(1)) if lo else None)


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return NM
    return {"n": len(xs), "mean": round(st.mean(xs), 3), "sd": round(st.pstdev(xs), 3) if len(xs) > 1 else 0.0,
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


def evaluate(run):
    case = next(c for c in CS.CASES if c["id"] == run["rev"]["case"])
    out = {"key": run["key"], "case": case["id"], "defect": case["defect"], "severity": case["severity"],
           "category": case["category"], "valid": run["rev"]["valid"]}
    verdict, hi, lo = review_header(run["rev"].get("result"))
    out.update(verdict=verdict, n_high=hi, n_low=lo, cost=run["rev"].get("cost_usd"), sec=run["rev"].get("sec"))
    p = (run["judge"] or {}).get("parsed")
    if not out["valid"] or p is None:
        out["status"] = "invalid" if not out["valid"] else "not_judged"
        return out
    classes = {f["label"]: f["class"] for f in p.get("findings", [])}
    classes.update(run["adj"].get("findings", {}))
    detected = run["adj"].get("defect_detected", p.get("defect_detected"))
    matched = run["adj"].get("matched_label", p.get("matched_label"))
    out.update(status="ok", classes=classes, detected=detected, matched=matched,
               judge_cost=(run["judge"] or {}).get("judge_cost_usd"))
    return out


def write(out):
    runs = [evaluate(r) for r in load(out)]
    ok = [r for r in runs if r["status"] == "ok"]
    defect = [r for r in ok if r["defect"]]
    clean = [r for r in ok if not r["defect"]]
    tp = [r for r in defect if r["detected"] == "yes"]
    fn = [r for r in defect if r["detected"] == "no"]
    partial = [r for r in defect if r["detected"] == "partial"]
    cls = lambda rs, c: sum(1 for r in rs for v in r["classes"].values() if v == c)  # noqa: E731
    fp = cls(ok, "false_positive")
    amb = cls(ok, "ambiguous")
    high = [r for r in defect if r["severity"] == "high"]
    high_tp = [r for r in high if r["detected"] == "yes"]
    tp_as_high = [r for r in tp if (r["matched"] or "").startswith("重大")]
    summary = {
        "conditions": json.load(open(os.path.join(out, "conditions.json"), encoding="utf-8"))
        if os.path.exists(os.path.join(out, "conditions.json")) else NM,
        "runs": {"total": len(runs), "judged": len(ok), "invalid_or_unjudged": [r["key"] for r in runs if r["status"] != "ok"]},
        "detection": {
            "TP": len(tp), "FN": len(fn), "FP": fp,
            "partial_unresolved": [r["key"] for r in partial], "ambiguous_findings_unresolved": amb,
            "precision": round(len(tp) / (len(tp) + fp), 3) if (tp or fp) else NM,
            "recall": round(len(tp) / (len(tp) + len(fn)), 3) if (tp or fn) else NM,
            "recall_reported_as_high": round(len(tp_as_high) / (len(tp) + len(fn)), 3) if (tp or fn) else NM,
            "high_severity_recall": round(len(high_tp) / len(high), 3) if high else NM,
            "high_severity_missed": sorted(r["key"] for r in high if r["detected"] == "no"),
            "missed": sorted(r["key"] for r in fn),
            "by_category": {c: f"{sum(1 for r in tp if r['category'] == c)}/{sum(1 for r in defect if r['category'] == c)}"
                            for c in sorted({r["category"] for r in defect})},
        },
        "clean_cases": {"n": len(clean), "verdict_needs_fix": sum(1 for r in clean if r["verdict"] == "要修正"),
                        "false_positive_findings": cls(clean, "false_positive"),
                        "cases_with_false_positive": sorted(r["key"] for r in clean
                                                            if "false_positive" in r["classes"].values())},
        "other_findings": {"real_other": cls(ok, "real_other"), "test_gap": cls(ok, "test_gap"), "style": cls(ok, "style")},
        "cost_usd_per_review": stats([r["cost"] for r in ok]),
        "sec_per_review": stats([r["sec"] for r in ok]),
        "judge_cost_usd_per_review": stats([r.get("judge_cost") for r in ok]),
        "human_confirmations_per_run": NM + "（レビュー担当だけを動かすため、人とのやりとりがない）",
        "not_measured": ["実装の品質（このベンチマークはレビューの検出だけを測る）", "人への確認回数",
                         "counterfactual・holdout・差分カバレッジを有効にした条件（既定の設定で測った）"],
    }
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    d = summary["detection"]
    lines = ["# 欠陥検出ベンチマークの結果", "",
             f"- 条件: {json.dumps(summary['conditions'], ensure_ascii=False)}",
             f"- レビュー: {len(runs)} 回（集計 {len(ok)}、無効・未判定 {len(runs) - len(ok)}）", "",
             "| 指標 | 値 |", "|---|---|",
             f"| TP / FN / FP | {d['TP']} / {d['FN']} / {d['FP']} |",
             f"| Recall | {d['recall']} |", f"| Precision | {d['precision']} |",
             f"| 重大として指摘した Recall | {d['recall_reported_as_high']} |",
             f"| 高重大度の Recall | {d['high_severity_recall']} |",
             f"| 高重大度の見逃し | {', '.join(d['high_severity_missed']) or 'なし'} |",
             f"| 欠陥なしケースで「要修正」 | {summary['clean_cases']['verdict_needs_fix']} / {summary['clean_cases']['n']} |",
             f"| 判定が未決（partial・ambiguous） | {len(d['partial_unresolved'])} 件・{d['ambiguous_findings_unresolved']} 件 |",
             f"| 1回の費用（USD） | {summary['cost_usd_per_review']} |",
             f"| 1回の時間（秒） | {summary['sec_per_review']} |",
             f"| 人への確認回数 | {summary['human_confirmations_per_run']} |", "",
             "## 分類ごとの検出", "", "| 分類 | 検出 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in d["by_category"].items()]
    lines += ["", "## ケースごと", "", "| run | 欠陥 | 重大度 | 判定 | 重大/軽微 | 検出 | 一致した指摘 | FP | real_other | test_gap | 費用 | 秒 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda x: x["key"]):
        if r["status"] != "ok":
            lines.append(f"| {r['key']} | {'あり' if r['defect'] else 'なし'} | {r['severity'] or '-'} | {r['status']} | | | | | | | | |")
            continue
        c = list(r["classes"].values())
        lines.append(f"| {r['key']} | {'あり' if r['defect'] else 'なし'} | {r['severity'] or '-'} | {r['verdict']} | "
                     f"{r['n_high']}/{r['n_low']} | {r['detected']} | {r['matched'] or '-'} | {c.count('false_positive')} | "
                     f"{c.count('real_other')} | {c.count('test_gap')} | {r['cost']:.2f} | {r['sec']:.0f} |")
    lines += ["", "## 測っていない項目", ""] + [f"- {x}" for x in summary["not_measured"]]
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:20]))
