"""クリーンケース群（EVAL-01）の集計。既存の40ケースの集計（report.py）とは別に出す。

  python3 bench/review/clean_report.py <DIR> [--out DIR/clean_report.md]

指標（要件書 EVAL-01）:
- クリーンケース正答率 = 「合格」になった回 / 全回
- 不要なブロック率 = 根拠なく「要修正」になった回 / 全回
  （重大の指摘が1件も real_other・test_gap と判定されなかった「要修正」。どれかが実在すると判定されたら、
  ケースの正解を疑って「要再調査」に数え、都合よく合格扱いにしない）
- 重大の誤検知（false_positive の [重大]）の件数を、対応区分（修正必須・証拠不足）ごとに。内容は一覧に出す
- 改善提案の数、報告の形式の不正（エンジンの parse_review が受け付けない報告。実際の run では書き直しになる）
"""
import json
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import agents as A  # noqa: E402

REAL = ("real_other", "test_gap")


def load(d):
    adj = {}
    p = os.path.join(d, "adjudication.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            adj = json.load(f)
    out = []
    for fn in sorted(os.listdir(os.path.join(d, "reviews"))):
        with open(os.path.join(d, "reviews", fn), encoding="utf-8") as f:
            rev = json.load(f)
        if not rev.get("valid"):
            out.append({"rev": rev, "status": "invalid_run"})
            continue
        jp = os.path.join(d, "judged", fn)
        judged = None
        if os.path.exists(jp):
            with open(jp, encoding="utf-8") as f:
                judged = json.load(f)
        out.append({"rev": rev, "judged": judged, "status": "ok" if judged else "not_judged",
                    "adj": adj.get(f"{rev['case']}#{rev['rep']}", {})})
    return out


def evaluate(r):
    rev = r["rev"]
    rec, errs = A.parse_review(rev.get("result") or "")
    classes = {}
    reasons = {}
    for f in (r["judged"] or {}).get("parsed", {}).get("findings", []):
        classes[f["label"]] = f["class"]
        reasons[f["label"]] = f.get("reason", "")
    classes.update(r.get("adj") or {})
    blocking = [f for f in rec["findings"] if f["severity"] == "重大"]
    minor = [f for f in rec["findings"] if f["severity"] == "軽微"]
    real_block = [f for f in blocking if classes.get(f["label"]) in REAL]
    return {"case": rev["case"], "rep": rev["rep"], "verdict": rec["verdict"], "format_errors": errs,
            "blocking": [dict(f, cls=classes.get(f["label"], "?"), reason=reasons.get(f["label"], "")) for f in blocking],
            "n_minor": len(minor), "n_suggestion": sum(1 for f in minor if f.get("action_type") == "suggestion"),
            "justified_block": bool(real_block),
            "cost": rev.get("cost_usd"), "sec": rev.get("sec")}


def ms(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "not_measured"
    return f"{st.mean(xs):.3f} ± {st.pstdev(xs):.3f}" if len(xs) > 1 else f"{xs[0]:.3f}"


def summarize(evs):
    n = len(evs)
    passed = sum(1 for e in evs if e["verdict"] == "合格")
    needs = [e for e in evs if e["verdict"] == "要修正"]
    unjust = [e for e in needs if not e["justified_block"]]
    fp = [(e, f) for e in evs for f in e["blocking"] if f["cls"] == "false_positive"]
    by_act = {}
    for _e, f in fp:
        k = {"fix_required": "修正必須", "missing_evidence": "証拠不足"}.get(f.get("action_type"), "区分なし")
        by_act[k] = by_act.get(k, 0) + 1
    return {"runs": n, "pass": passed, "pass_rate": passed / n if n else None,
            "needs_fix": len(needs), "unjustified_block": len(unjust),
            "unjustified_block_rate": len(unjust) / n if n else None,
            "to_reinvestigate": len(needs) - len(unjust),
            "blocking_fp": len(fp), "blocking_fp_by_action": by_act,
            "blocking_findings": sum(len(e["blocking"]) for e in evs),
            "suggestions": sum(e["n_suggestion"] for e in evs), "minor": sum(e["n_minor"] for e in evs),
            "format_invalid": sum(1 for e in evs if e["format_errors"]),
            "unparsed_verdict": sum(1 for e in evs if e["verdict"] is None),
            "cost": ms([e["cost"] for e in evs]), "sec": ms([e["sec"] for e in evs])}


def write(d, out=None):
    rows = load(d)
    evs = [evaluate(r) for r in rows if r["status"] == "ok"]
    sm = summarize(evs)
    sm["not_judged"] = sum(1 for r in rows if r["status"] == "not_judged")
    sm["invalid_runs"] = sum(1 for r in rows if r["status"] == "invalid_run")
    f = lambda v: "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    lines = ["# クリーンケース群の結果（EVAL-01）", "",
             "| 指標 | 値 |", "|---|---|",
             f"| 回数（判定済み） | {sm['runs']}（未判定 {sm['not_judged']}・無効な実行 {sm['invalid_runs']}） |",
             f"| クリーンケース正答率（合格） | {f(sm['pass_rate'])}（{sm['pass']}/{sm['runs']}） |",
             f"| 不要なブロック率 | {f(sm['unjustified_block_rate'])}（{sm['unjustified_block']}/{sm['runs']}） |",
             f"| 要再調査（重大の指摘が実在と判定された「要修正」） | {sm['to_reinvestigate']} |",
             f"| 重大の指摘 | {sm['blocking_findings']} |",
             f"| 重大の誤検知 | {sm['blocking_fp']}（{json.dumps(sm['blocking_fp_by_action'], ensure_ascii=False)}） |",
             f"| 軽微の指摘（うち改善提案） | {sm['minor']}（{sm['suggestions']}） |",
             f"| 報告の形式の不正（エンジンが受け付けない） | {sm['format_invalid']} |",
             f"| 判定を読み取れない | {sm['unparsed_verdict']} |",
             f"| 1回の費用（USD） | {sm['cost']} |", f"| 1回の時間（秒） | {sm['sec']} |", "",
             "## ケースごと", "", "| ケース | 回 | 判定 | 重大（分類） | 軽微 | 形式 |", "|---|---|---|---|---|---|"]
    for e in evs:
        bl = "、".join(f"{x['label']}:{x.get('action_type') or '-'}:{x['cls']}" for x in e["blocking"]) or "-"
        lines.append(f"| {e['case']} | {e['rep']} | {e['verdict']} | {bl} | {e['n_minor']} | "
                     f"{'不正' if e['format_errors'] else 'OK'} |")
    lines += ["", "## 重大の指摘の一覧（判定役の理由）", ""]
    for e in evs:
        for x in e["blocking"]:
            text = re.sub(r"\s+", " ", x["text"])[:200]
            lines.append(f"- {e['case']}#{e['rep']} [{x['label']}] {x.get('action_type') or '-'} / {x['cls']}: "
                         f"{text}  \n  判定役: {x['reason']}")
    md = "\n".join(lines) + "\n"
    with open(out or os.path.join(d, "clean_report.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(os.path.join(d, "clean_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(sm, fh, ensure_ascii=False, indent=1)
    print(md[:3000])
    return sm


if __name__ == "__main__":
    write(sys.argv[1], sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None)
