"""同じ根本原因の指摘を、両方の条件で同じ分類に揃える（規則は ANALYSIS に記録する）。"""
import json, os, re, sys
from collections import Counter
TX = r"(トランザクション|transaction\(?\)?|BEGIN( IMMEDIATE)?)"
TOCTOU = lambda t: re.search(TX + r"[^。]{0,6}(の)?(外|前)", t) and re.search(r"認可|権限|_can_modify|_visible|可視性", t)
ZERO = lambda t: re.search(r"先頭(に|の|が)?\s*(0|ゼロ|０)", t) and re.search(r"リンク|note_id|署名", t)
OVER = lambda t: re.search(r"OverflowError|桁あふれ|整数の範囲|巨大な|極端に大きな|非常に大きな|10\*\*", t)
APPLY = {"false_positive", "real_other", "ambiguous"}
# 使い方: python3 adjudicate_rules.py（変更前・Phase 1 の計測フォルダを /tmp/claude-0/ に置いた状態で実行した。パスは計測のときのもの）
manual = {"bench-v066": {"BND-04#2": {"軽微1": "false_positive"}, "TX-04#3": {"軽微1": "matches_defect"}}, "bench-p1": {}}
for d in ("bench-v066", "bench-p1"):
    O = f"/tmp/claude-0/{d}"
    adj = json.load(open(f"{O}/adjudication.json")) if os.path.exists(f"{O}/adjudication.json") else {}
    changes, left = [], []
    for f in sorted(os.listdir(f"{O}/judged")):
        key = f[:-5].replace(".", "#")
        if d == "bench-v066" and key.endswith("#1"):
            continue
        j = json.load(open(f"{O}/judged/{f}"))["parsed"]; rv = json.load(open(f"{O}/reviews/{f}"))["result"] or ""
        for fd in j["findings"]:
            lab = fd["label"]; cur = adj.get(key, {}).get("findings", {}).get(lab, fd["class"])
            m = re.search(r"- \[" + re.escape(lab) + r"\][^\n]*", rv); t = m.group(0) if m else ""
            new = None
            if key in manual[d] and lab in manual[d][key]:
                new = manual[d][key][lab]
            elif cur in APPLY:
                if TOCTOU(t): new = "real_other"
                elif ZERO(t): new = "real_other" if OVER(t) else "false_positive"
                elif cur == "ambiguous" and OVER(t): new = "real_other"
            if new and new != cur:
                adj.setdefault(key, {}).setdefault("findings", {})[lab] = new; changes.append((key, lab, cur, new))
            if adj.get(key, {}).get("findings", {}).get(lab, fd["class"]) == "ambiguous":
                left.append((key, lab))
    json.dump(adj, open(f"{O}/adjudication.json", "w"), ensure_ascii=False, indent=1)
    print(d, len(changes), dict(Counter((c[2], c[3]) for c in changes)), "残った ambiguous:", left)
