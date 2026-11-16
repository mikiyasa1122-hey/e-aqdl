"""レビュー品質の記録と集計（改善C）。指摘の件数ではなく、妥当性・重大度・見逃し・誤検知・コストを追う。

正本は run の state:
- 内部の指摘: reviews[].findings（独立レビューが出したもの。キーは「RV<レビュー番号>.<ラベル>」）
- 判定: finding_judgments[キー] = {judgment, of, by, reason, seq, at}
  - judgment: valid（妥当）/ false_positive（誤検知）/ needs_check（要検証）/ duplicate（重複。of が元のキー）/
    accepted_risk（受容済みリスク: 指摘は妥当だが、仕様上の判断で直さない。誤検知とは別）
  - 判定がなければ「未判定」（誤検知とは数えない）
- 対応: finding_resolutions[キー] = {status: fixed|accepted|open, commit, test, by, reason, seq, at}
  - 記録がなくても、そのラベルを修正依頼として受けた実装の回の後に、合格したレビューがあれば「修正済み（推定）」と出す
- 外部の追加発見: external_findings[]（外部レビュー・人・実運用。EXT-<n>）
  - missed: yes（内部レビューが検出できたはずの、確定した見逃し）/ no / unknown
  - same_as: 内部の指摘と同じなら、そのキー（見逃しではない）

重複の扱い: 明示の duplicate に加え、後のレビューが同じ対象・同じ本文の指摘を繰り返したもの（直っていない指摘の再掲）は、
自動で最初の指摘の重複として扱う。集計は重複を除いた件数で行う。

数え方の注意: 監査対象の「本当の欠陥の総数」は分からないので、再現率（recall）は出さない。代わりに
「観測された追加発見率 = 外部で妥当と分かった追加の件数 / (内部の妥当な指摘 + 外部の追加)」を、母集団と判定済みの件数を
添えて出す。未判定がある間の値は「暫定」と表示する（確定値と混ぜない）。

このモジュールは状態を書き換えない純粋関数だけを持つ。
"""
import re

JUDGMENTS = ("valid", "false_positive", "needs_check", "duplicate", "accepted_risk")
JUDGMENT_LABEL = {"valid": "妥当", "false_positive": "誤検知", "needs_check": "要検証", "duplicate": "重複",
                  "accepted_risk": "受容済みリスク", None: "未判定"}
RESOLUTIONS = ("fixed", "accepted", "open")
RESOLUTION_LABEL = {"fixed": "修正済み", "accepted": "受容（直さない）", "open": "未対応"}
SOURCES = ("external_review", "human", "production")
SOURCE_LABEL = {"external_review": "外部レビュー", "human": "人", "production": "実運用", "internal": "内部レビュー"}
SEVERITIES = ("high", "medium", "low")
KEY_RE = re.compile(r"^(RV\d+\.(?:重大|軽微)\d+|EXT-\d+)$")


def _norm(t):
    return re.sub(r"\s+", "", t or "")


def _targets(text):
    m = re.search(r"対象\s*[:：]\s*([^/／]*)", text or "")
    return tuple(sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9_.-]*", m.group(1))))) if m else ()


def _body(text):
    """自動の重複判定に使う本文（「内容」の欄。なければ全体）。"""
    m = re.search(r"内容\s*[:：]\s*([^/／]*)", text or "")
    return _norm(m.group(1) if m else text)


def internal(state):
    """内部の指摘の一覧（有効なレビューだけ）。各要素: key・review・label・severity・action_type・targets・text・at。"""
    out = []
    for rv in state.get("reviews", []):
        if not rv.get("valid"):
            continue
        for f in rv.get("findings", []):
            out.append({"key": f"RV{rv['n']}.{f['label']}", "review": rv["n"], "label": f["label"],
                        "severity": f.get("severity"), "action_type": f.get("action_type"),
                        "targets": _targets(f.get("text")), "text": f.get("text", ""), "at": rv.get("at"),
                        "source": "internal"})
    return out


def keys(state):
    return {f["key"] for f in internal(state)} | {x["id"] for x in state.get("external_findings", [])}


def auto_duplicates(items):
    """後のレビューが繰り返した同じ指摘（対象と本文が同じ）→ {後のキー: 最初のキー}。"""
    first, dup = {}, {}
    for f in items:
        sig = (f["severity"], f["targets"], _body(f["text"]))
        if not sig[2]:
            continue
        if sig in first and first[sig]["review"] != f["review"]:
            dup[f["key"]] = first[sig]["key"]
        else:
            first.setdefault(sig, f)
    return dup


def inferred_fixed(state):
    """修正依頼として受けた実装の回の後に、合格した有効なレビューがあるラベル → 推定で修正済み。"""
    out = set()
    rounds = (state.get("impl") or {}).get("rounds", [])
    reviews = [r for r in state.get("reviews", []) if r.get("valid")]
    for rd in rounds:
        fx = rd.get("fix") or {}
        if fx.get("kind") != "review":
            continue
        if any(r.get("passed") and r["seq"] > rd["seq"] for r in reviews):
            out |= {f"RV{fx.get('ref')}.{lab}" for lab in fx.get("labels", [])}
    return out


def rows(state):
    """指摘ごとの記録（内部 + 外部）。判定・対応・重複を解決したもの。"""
    items = internal(state)
    auto = auto_duplicates(items)
    judg = state.get("finding_judgments") or {}
    res = state.get("finding_resolutions") or {}
    fixed_guess = inferred_fixed(state)
    out = []
    for f in items:
        j = judg.get(f["key"]) or {}
        jd = j.get("judgment")
        of = j.get("of")
        if not jd and f["key"] in auto:
            jd, of = "duplicate", auto[f["key"]]
        r = res.get(f["key"]) or {}
        st = r.get("status") or ("fixed" if f["key"] in fixed_guess else None)
        out.append(dict(f, judgment=jd, of=of, judged_by=j.get("by") or ("auto" if f["key"] in auto and not j else None),
                        resolution=st, resolution_inferred=bool(st and not r.get("status")),
                        commit=r.get("commit"), test=r.get("test")))
    for x in state.get("external_findings", []):
        j = judg.get(x["id"]) or {}
        r = res.get(x["id"]) or {}
        out.append({"key": x["id"], "source": x.get("source"), "severity": x.get("severity"), "targets": tuple(
            x.get("targets") or ()), "text": x.get("text", ""), "at": x.get("at"),
            "judgment": j.get("judgment") or x.get("judgment"),  # 追加の時点で判定しなければ未判定
            "of": j.get("of"), "judged_by": j.get("by") or x.get("by"), "resolution": r.get("status"),
            "resolution_inferred": False, "missed": x.get("missed", "unknown"), "same_as": x.get("same_as"),
            "cause": x.get("cause"), "prevention": x.get("prevention"), "commit": r.get("commit"), "test": r.get("test")})
    return out


def summarize(state, reviewer_sec=None, reviewer_tokens=None):
    """1つの run の集計。recall は出さない。未判定が残る値は provisional=True。"""
    rs = rows(state)
    internal_rows = [r for r in rs if r["source"] == "internal"]
    ext = [r for r in rs if r["source"] != "internal"]
    uniq = [r for r in internal_rows if r["judgment"] != "duplicate"]
    cnt = {k: sum(1 for r in uniq if r["judgment"] == k) for k in JUDGMENTS if k != "duplicate"}
    untriaged = sum(1 for r in uniq if r["judgment"] is None)
    judged_tf = cnt["valid"] + cnt["false_positive"] + cnt["accepted_risk"]
    by_sev = {}
    for sev in ("重大", "軽微"):
        g = [r for r in uniq if r["severity"] == sev]
        v = sum(1 for r in g if r["judgment"] in ("valid", "accepted_risk"))
        fp = sum(1 for r in g if r["judgment"] == "false_positive")
        by_sev[sev] = {"detected": len(g), "valid": v, "false_positive": fp,
                       "untriaged": sum(1 for r in g if r["judgment"] is None),
                       "valid_rate": round(v / (v + fp), 3) if v + fp else None}
    ext_valid = [r for r in ext if r["judgment"] in ("valid", "accepted_risk") and not r.get("same_as")]
    valid_internal = cnt["valid"] + cnt["accepted_risk"]
    reviews = [r for r in state.get("reviews", []) if r.get("valid")]
    fixed = sum(1 for r in uniq if r["judgment"] == "valid" and r["resolution"] == "fixed")
    return {
        "detected_total": len(internal_rows),
        "detected_unique": len(uniq),
        "duplicates": len(internal_rows) - len(uniq),
        "duplicate_rate": round((len(internal_rows) - len(uniq)) / len(internal_rows), 3) if internal_rows else None,
        "judged": len(uniq) - untriaged, "untriaged": untriaged,
        "provisional": untriaged > 0,
        "valid": cnt["valid"], "false_positive": cnt["false_positive"], "needs_check": cnt["needs_check"],
        "accepted_risk": cnt["accepted_risk"],
        # 妥当 = 妥当 + 受容済みリスク（指摘そのものは正しい）。分母は妥当・誤検知の判定が済んだものだけ
        "precision": round(valid_internal / (valid_internal + cnt["false_positive"]), 3)
        if valid_internal + cnt["false_positive"] else None,
        "precision_denominator": valid_internal + cnt["false_positive"],
        "by_severity": by_sev,
        "fixed": fixed, "fix_rate": round(fixed / cnt["valid"], 3) if cnt["valid"] else None,
        "fixed_inferred": sum(1 for r in uniq if r["resolution"] == "fixed" and r["resolution_inferred"]),
        "external_additional": len(ext_valid),
        "external_by_source": {s: sum(1 for r in ext_valid if r["source"] == s) for s in SOURCES},
        "confirmed_misses": sum(1 for r in ext_valid if r.get("missed") == "yes"),
        "observed_additional_rate": round(len(ext_valid) / (valid_internal + len(ext_valid)), 3)
        if valid_internal + len(ext_valid) else None,
        "observed_additional_rate_note": "外部で妥当と分かった追加 / (内部の妥当な指摘 + 外部の追加)。"
                                         "本当の欠陥の総数は分からないので、再現率ではない",
        "reviews": len(reviews),
        "valid_per_review": round(valid_internal / len(reviews), 2) if reviews else None,
        "reviewer_sec": reviewer_sec, "reviewer_tokens": reviewer_tokens,
        "judged_ratio": round((len(uniq) - untriaged) / len(uniq), 3) if uniq else None,
    }


def aggregate(runs):
    """複数の run（[(run_id, summary)]）をまとめる。判定済みの件数だけで率を出し、未判定の数を添える。"""
    keys_ = ("detected_unique", "duplicates", "judged", "untriaged", "valid", "false_positive", "needs_check",
             "accepted_risk", "fixed", "external_additional", "confirmed_misses", "reviews")
    tot = {k: sum((m.get(k) or 0) for _r, m in runs) for k in keys_}
    v = tot["valid"] + tot["accepted_risk"]
    tot["precision"] = round(v / (v + tot["false_positive"]), 3) if v + tot["false_positive"] else None
    tot["observed_additional_rate"] = (round(tot["external_additional"] / (v + tot["external_additional"]), 3)
                                       if v + tot["external_additional"] else None)
    tot["fix_rate"] = round(tot["fixed"] / tot["valid"], 3) if tot["valid"] else None
    tot["valid_per_review"] = round(v / tot["reviews"], 2) if tot["reviews"] else None
    tot["provisional"] = tot["untriaged"] > 0
    tot["runs"] = len(runs)
    return tot
