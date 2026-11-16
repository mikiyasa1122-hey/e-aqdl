"""レビュー品質の記録のコマンド（統括と人が使う。サブエージェントは使えない）。規則と集計は review_quality.py。

- findings                         指摘の一覧（判定・対応・重複・外部の追加発見）と、この run の集計
- finding-judge <キー> <判定>       妥当・誤検知・要検証・重複（--of <元のキー>）・受容済みリスク
- finding-resolve <キー> <状態>     修正済み（--commit・--test）・受容（直さない）・未対応
- finding-add "<内容>" --source …  外部レビュー・人・実運用で見つかった追加の問題（EXT-n）

run が完了した後（PR のマージ後の外部レビュー・実運用）にも記録できる。記録は追記で、gate・レビューの判定には影響しない。
秘密情報・認証情報・業務データの本文は書かない（それらしい値があれば拒否する）。
"""
from .. import approval as AP
from .. import review_quality as RQ
from ..state import actor_hint, bump, log, save_state
from ..util import die, info, now_iso, pop_flag, pop_opt
from . import open_state


def _secret_check(project, *texts):
    from ..memory import sensitive_findings
    for t in texts:
        hit = sensitive_findings(t or "", project)
        if hit:
            die("秘密情報・認証情報らしい値があるため記録しません（" + "、".join(str(h) for h in hit[:3])
                + "）。値を伏せて、何が起きたかだけを書いてください。")


def _by(s, project, key, by):
    """判定・対応をした人。--by human は、人の返答にそのキーがあることを確かめる（会話記録で）。"""
    if by in (None, "", "orchestrator"):
        return actor_hint(project) if actor_hint(project) != "other" else "orchestrator"
    if by != "human":
        die("--by は human か orchestrator")
    for e in reversed(s.get("human_events", [])):
        if key in (e.get("text") or "") and (not s.get("eaqdl") or AP.in_transcript(s, e)):
            return "human"
    die(f"人の返答に {key} がありません。人の判定として記録するなら、人に {key} の判定を聞いてから記録してください。")


def cmd_finding_judge(args):
    of = pop_opt(args, "--of")
    reason = pop_opt(args, "--reason") or ""
    by = pop_opt(args, "--by")
    project, s, spath = open_state(args)
    if len(args) != 2:
        die("使い方: finding-judge <RV1.重大1|EXT-1> valid|false_positive|needs_check|duplicate|accepted_risk "
            '[--of <元のキー>] --reason "<理由>" [--by human]')
    key, judgment = args
    if key not in RQ.keys(s):
        die(f"指摘 {key} がありません（findings で確認）。")
    if judgment not in RQ.JUDGMENTS:
        die("判定は " + " / ".join(RQ.JUDGMENTS))
    if judgment == "duplicate":
        if not of or of not in RQ.keys(s) or of == key:
            die("重複は --of <元の指摘のキー> を付ける（自分以外の、ある指摘）")
    elif of:
        die("--of は重複（duplicate）のときだけ")
    if not reason.strip():
        die("--reason に判定の理由を書く")
    _secret_check(project, reason)
    rec = {"judgment": judgment, "of": of, "by": _by(s, project, key, by), "reason": reason,
           "seq": bump(s), "at": now_iso()}
    old = (s.setdefault("finding_judgments", {})).get(key)
    if old:
        rec["previous"] = {k: old.get(k) for k in ("judgment", "by", "at")}
    s["finding_judgments"][key] = rec
    log(s, project, "finding.judge", {"key": key, "judgment": judgment, "of": of, "by": rec["by"]})
    save_state(spath, s)
    info(f"{key}: {RQ.JUDGMENT_LABEL[judgment]}（{rec['by']}）")


def cmd_finding_resolve(args):
    commit = pop_opt(args, "--commit")
    test = pop_opt(args, "--test")
    reason = pop_opt(args, "--reason") or ""
    by = pop_opt(args, "--by")
    project, s, spath = open_state(args)
    if len(args) != 2:
        die('使い方: finding-resolve <キー> fixed|accepted|open [--commit <SHA>] [--test <テスト>] --reason "<理由>"')
    key, status = args
    if key not in RQ.keys(s):
        die(f"指摘 {key} がありません。")
    if status not in RQ.RESOLUTIONS:
        die("状態は " + " / ".join(RQ.RESOLUTIONS))
    if status == "fixed" and not (commit or test):
        die("修正済みには、修正のコミット（--commit）か、確かめたテスト（--test）を付ける")
    if status == "accepted" and not reason.strip():
        die("受容（直さない）には --reason を付ける")
    _secret_check(project, reason)
    rec = {"status": status, "commit": commit, "test": test, "reason": reason, "by": _by(s, project, key, by),
           "seq": bump(s), "at": now_iso()}
    s.setdefault("finding_resolutions", {})[key] = rec
    log(s, project, "finding.resolve", {"key": key, "status": status, "commit": commit, "test": test})
    save_state(spath, s)
    info(f"{key}: {RQ.RESOLUTION_LABEL[status]}")


def cmd_finding_add(args):
    source = pop_opt(args, "--source")
    severity = pop_opt(args, "--severity")
    targets = [t.strip() for t in (pop_opt(args, "--target") or "").split(",") if t.strip()]
    missed = pop_opt(args, "--missed") or "unknown"
    cause = pop_opt(args, "--cause")
    prevention = pop_opt(args, "--prevention")
    same_as = pop_opt(args, "--same-as")
    judgment = pop_opt(args, "--judge")
    project, s, spath = open_state(args)
    text = " ".join(args).strip()
    usage = ('使い方: finding-add "<内容>" --source external_review|human|production --severity high|medium|low '
             '[--target R1,K2] [--judge valid|false_positive|needs_check|accepted_risk] [--missed yes|no|unknown] '
             '[--cause "<内部レビューで見つからなかった原因>"] [--prevention "<再発防止>"] [--same-as RV1.重大1]')
    if not text or source not in RQ.SOURCES or severity not in RQ.SEVERITIES:
        die(usage)
    if missed not in ("yes", "no", "unknown"):
        die("--missed は yes / no / unknown")
    if judgment and judgment not in RQ.JUDGMENTS or judgment == "duplicate":
        die("--judge は valid / false_positive / needs_check / accepted_risk（重複は --same-as で内部の指摘を示す）")
    if same_as and same_as not in RQ.keys(s):
        die(f"内部の指摘 {same_as} がありません。")
    if same_as and missed == "yes":
        die("内部の指摘と同じ（--same-as）なら、見逃しではありません（--missed no）")
    if missed == "yes" and not (cause and prevention):
        die("確定した見逃し（--missed yes）には、原因（--cause）と再発防止（--prevention）を書く")
    _secret_check(project, text, cause, prevention)
    n = len(s.setdefault("external_findings", [])) + 1
    eid = f"EXT-{n}"
    s["external_findings"].append({"id": eid, "source": source, "severity": severity, "targets": targets,
                                   "text": text, "missed": "no" if same_as else missed, "cause": cause,
                                   "prevention": prevention, "same_as": same_as, "judgment": judgment,
                                   "by": actor_hint(project), "seq": bump(s), "at": now_iso()})
    log(s, project, "finding.add", {"id": eid, "source": source, "severity": severity, "missed": missed})
    save_state(spath, s)
    info(f"{eid}（{RQ.SOURCE_LABEL[source]}・{severity}）を記録しました"
         + ("。判定は finding-judge で" if not judgment else ""))


def cmd_findings(args):
    as_json = pop_flag(args, "--json")
    project, s, _ = open_state(args)
    rows = RQ.rows(s)
    from .metrics import collect_metrics
    m = collect_metrics(s, project)["review_quality"]
    if as_json:
        import json
        print(json.dumps({"findings": [dict(r, targets=list(r["targets"])) for r in rows], "summary": m},
                         ensure_ascii=False, indent=2))
        return
    print("| キー | 発見元 | 重要度 | 判定 | 対応 | 対象 | 内容 |\n|---|---|---|---|---|---|---|")
    for r in rows:
        jd = RQ.JUDGMENT_LABEL[r["judgment"]] + (f"（{r['of']} の）" if r["judgment"] == "duplicate" else "") \
            + (f"・{r['judged_by']}" if r.get("judged_by") else "")
        rs = (RQ.RESOLUTION_LABEL.get(r["resolution"], "-") + ("（推定）" if r["resolution_inferred"] else "")
              if r["resolution"] else "-")
        extra = ("・見逃し" if r.get("missed") == "yes" else "") if r["source"] != "internal" else ""
        body = r["text"].replace("|", "／")[:80]
        print(f"| {r['key']} | {RQ.SOURCE_LABEL.get(r['source'], r['source'])}{extra} | {r['severity']} | {jd} | {rs} | "
              f"{', '.join(r['targets'])} | {body} |")
    print()
    print(summary_text(m))


def summary_text(m):
    tag = "暫定（未判定あり）" if m["provisional"] else "確定"
    lines = [f"レビュー品質（{tag}）: 指摘 {m['detected_unique']} 件（重複を除く。重複 {m['duplicates']} 件）・"
             f"判定済み {m['judged']}・未判定 {m['untriaged']}",
             f"- 妥当 {m['valid']}・受容済みリスク {m['accepted_risk']}・誤検知 {m['false_positive']}・要検証 {m['needs_check']}",
             "- 指摘の適合率: " + (f"{m['precision']}（判定済み {m['precision_denominator']} 件が分母）" if m["precision"] is not None
                                 else "算出できず（妥当・誤検知の判定がない）"),
             "- 重要度別の妥当率: " + "、".join(f"{k} {v['valid_rate'] if v['valid_rate'] is not None else '-'}"
                                          f"（{v['valid']}/{v['valid'] + v['false_positive']}・未判定 {v['untriaged']}）"
                                          for k, v in m["by_severity"].items()),
             f"- 修正率: {m['fix_rate'] if m['fix_rate'] is not None else '-'}（修正済み {m['fixed']}、うち推定 {m['fixed_inferred']}）"
             f"・重複率: {m['duplicate_rate'] if m['duplicate_rate'] is not None else '-'}",
             f"- 外部の追加発見: {m['external_additional']} 件（" + "・".join(
                 f"{RQ.SOURCE_LABEL[k]} {v}" for k, v in m["external_by_source"].items()) + f"）、確定した見逃し {m['confirmed_misses']} 件",
             "- 観測された追加発見率: " + (f"{m['observed_additional_rate']}" if m["observed_additional_rate"] is not None else "-")
             + f"（{m['observed_additional_rate_note']}）",
             f"- レビュー {m['reviews']} 回・1回あたりの妥当な指摘 {m['valid_per_review'] if m['valid_per_review'] is not None else '-'}"
             + (f"・レビューの時間 {round(m['reviewer_sec'])} 秒" if m.get("reviewer_sec") else "")]
    return "\n".join(lines)
