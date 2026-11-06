"""2つの条件（同じケース・同じモデル）のベンチマークの結果を比べる。

  python3 bench/review/compare.py <変更前の DIR> <変更後の DIR> [--out compare.md]

数え方は report.py と同じ（手での判定 adjudication.json を使う）。加えて次を出す:
- 回（rep）ごとの precision・recall・高重大度の recall と、その平均・標準偏差
- ケースごとの検出の回数（例: 3/3）。変更後に検出が減ったケースを挙げる
- 誤検知を重大と軽微に分けた数（重大の誤検知は修正ループを起こすため、必要な修正だけを求めるかの指標になる）
- 対応区分（修正必須・証拠不足・改善提案）が報告に書かれているか（変更後の指示に従っているか）
"""
import json
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import report as RP  # noqa: E402


def runs(d):
    out = []
    for r in RP.load(d):
        e = RP.evaluate(r)
        e["review"] = r["rev"].get("result") or ""
        e["rep"] = r["rev"]["rep"]
        out.append(e)
    return [e for e in out if e["status"] == "ok"]


def ms(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "not_measured"
    return f"{st.mean(xs):.3f} ± {st.pstdev(xs):.3f}" if len(xs) > 1 else f"{xs[0]:.3f}"


def metrics(rs):
    d = [r for r in rs if r["defect"]]
    tp = sum(1 for r in d if r["detected"] == "yes")
    fn = sum(1 for r in d if r["detected"] != "yes")
    fp_hi = sum(1 for r in rs for k, v in r["classes"].items() if v == "false_positive" and k.startswith("重大"))
    fp_lo = sum(1 for r in rs for k, v in r["classes"].items() if v == "false_positive" and not k.startswith("重大"))
    hi = [r for r in d if r["severity"] == "high"]
    count = lambda c: sum(1 for r in rs for v in r["classes"].values() if v == c)  # noqa: E731
    clean = [r for r in rs if not r["defect"]]
    return {"runs": len(rs), "TP": tp, "FN": fn, "FP": fp_hi + fp_lo, "FP_high": fp_hi, "FP_low": fp_lo,
            "precision": tp / (tp + fp_hi + fp_lo) if tp + fp_hi + fp_lo else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "high_recall": sum(1 for r in hi if r["detected"] == "yes") / len(hi) if hi else None,
            "real_other": count("real_other"), "test_gap": count("test_gap"), "style": count("style"),
            "ambiguous": count("ambiguous"), "partial": sum(1 for r in d if r["detected"] == "partial"),
            "clean_needs_fix": sum(1 for r in clean if r["verdict"] == "要修正"), "clean_runs": len(clean),
            "clean_with_blocking_fp": sum(1 for r in clean if any(v == "false_positive" and k.startswith("重大")
                                                                 for k, v in r["classes"].items())),
            "n_high_per_run": st.mean([r["n_high"] or 0 for r in rs]) if rs else None,
            "n_low_per_run": st.mean([r["n_low"] or 0 for r in rs]) if rs else None,
            "cost": [r["cost"] for r in rs], "sec": [r["sec"] for r in rs]}


def action_types(rs):
    """報告の指摘行に書かれた対応区分の数と、指摘の分類との対応。"""
    out = {}
    for r in rs:
        for line in r["review"].splitlines():
            m = re.match(r"^\s*[-*]?\s*\[(重大|軽微)(\d+)\]", line)
            if not m:
                continue
            a = re.search(r"対応区分\s*[:：]\s*(修正必須|証拠不足|改善提案)", line)
            key = (m.group(1), a.group(1) if a else "なし")
            cls = r["classes"].get(m.group(1) + m.group(2), "?")
            out.setdefault(key, {}).setdefault(cls, 0)
            out[key][cls] += 1
    return out


def per_case(rs):
    out = {}
    for r in rs:
        if r["defect"]:
            k = out.setdefault(r["case"], [0, 0])
            k[1] += 1
            k[0] += r["detected"] == "yes"
    return out


def table(name_a, ra, name_b, rb):
    ma, mb = metrics(ra), metrics(rb)
    reps = sorted({r["rep"] for r in ra} | {r["rep"] for r in rb})
    per_rep = {n: [metrics([r for r in rs if r["rep"] == k]) for k in reps if any(r["rep"] == k for r in rs)]
               for n, rs in ((name_a, ra), (name_b, rb))}
    f = lambda v: "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    rows = [("回数（runs）", "runs"), ("TP", "TP"), ("FN（見逃し）", "FN"), ("FP（誤検知）", "FP"),
            ("　うち重大（誤った修正必須・証拠不足）", "FP_high"), ("　うち軽微", "FP_low"),
            ("Precision", "precision"), ("Recall", "recall"), ("高重大度の Recall", "high_recall"),
            ("別の実在の欠陥（real_other）", "real_other"), ("テスト不足の指摘（test_gap）", "test_gap"),
            ("スタイル（style）", "style"), ("未決（ambiguous）", "ambiguous"), ("partial（未決）", "partial"),
            ("欠陥なしで「要修正」", "clean_needs_fix"), ("欠陥なしで重大の誤検知がある回", "clean_with_blocking_fp"),
            ("1回の重大の指摘の数", "n_high_per_run"), ("1回の軽微の指摘の数", "n_low_per_run")]
    lines = [f"| 指標 | {name_a} | {name_b} |", "|---|---|---|"]
    lines += [f"| {lab} | {f(ma[k])} | {f(mb[k])} |" for lab, k in rows]
    for lab, k in (("Precision（回ごと）", "precision"), ("Recall（回ごと）", "recall"),
                   ("高重大度の Recall（回ごと）", "high_recall")):
        lines.append(f"| {lab} 平均 ± 標準偏差 | {ms([m[k] for m in per_rep[name_a]])} | "
                     f"{ms([m[k] for m in per_rep[name_b]])} |")
    lines.append(f"| 1回の費用（USD） | {ms(ma['cost'])} | {ms(mb['cost'])} |")
    lines.append(f"| 1回の時間（秒） | {ms(ma['sec'])} | {ms(mb['sec'])} |")
    pa, pb = per_case(ra), per_case(rb)
    worse = sorted(c for c in pa if c in pb and pb[c][0] / pb[c][1] < pa[c][0] / pa[c][1])
    better = sorted(c for c in pa if c in pb and pb[c][0] / pb[c][1] > pa[c][0] / pa[c][1])
    lines += ["", "## ケースごとの検出（検出した回 / 回数）", "",
              f"| ケース | {name_a} | {name_b} |", "|---|---|---|"]
    lines += [f"| {c} | {pa[c][0]}/{pa[c][1]} | {pb.get(c, [0, 0])[0]}/{pb.get(c, [0, 0])[1]} |" for c in sorted(pa)
              if pa[c][0] != pa[c][1] or pb.get(c, [1, 1])[0] != pb.get(c, [1, 1])[1]]
    lines += ["", f"- {name_b} で検出が減ったケース: {', '.join(worse) or 'なし'}",
              f"- {name_b} で検出が増えたケース: {', '.join(better) or 'なし'}"]
    for n, rs in ((name_a, ra), (name_b, rb)):
        at = action_types(rs)
        lines += ["", f"## 対応区分の記載と、指摘の分類（{n}）", "", "| 重大/軽微 | 対応区分 | 分類ごとの数 |", "|---|---|---|"]
        lines += [f"| {k[0]} | {k[1]} | {json.dumps(v, ensure_ascii=False)} |" for k, v in sorted(at.items())]
    return "\n".join(lines), ma, mb, worse


if __name__ == "__main__":
    a, b = sys.argv[1], sys.argv[2]
    md, ma, mb, worse = table("変更前（v0.6.6）", runs(a), "変更後（Phase 1）", runs(b))
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    if out:
        open(out, "w", encoding="utf-8").write("# 比較\n\n" + md + "\n")
    print(md)
