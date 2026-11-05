"""検証結果の契約と、gate の合否の規則（純粋関数。外部コマンドを実行しない）。

実行する側（gate.run_gate・holdout・quality）は、ここの関数で結果の記録を作る。
読む側（state の問題の検出・状態の検証、agents の修正依頼、gate の最終判定）は、生の辞書を独自に解釈せず、
ここの関数で「合格か」「gate を止めるか」を判断する。パッケージ内の他のモジュールには依存しない。

結果の記録（state.json の gate.results.* の各値）:
- status: 実行・評価の状態
    passed         実行して合格
    failed         実行して不合格
    error          実行できなかった（タイムアウト・コマンドが見つからない・レポートを読めない など）
    skipped        前提の不合格などで実行しなかった
    not_applicable 対象のファイルがなく、実行しなかった
    pending        マージ後に実行する
    manual         人の確認が必要（機械では確かめていない）
- pass: 後方互換。**status が passed のときだけ true**（実行していないものを合格と書かない）
- reason: 合格以外の理由（日本語）
- scope: {"mode": "all" | "changed", "targets": 対象ファイル数}（{changed} を使ったときだけ "changed"）
- optional: 対象がないときに gate を止めない種類か（Lint・型チェックだけ）
- 旧版の記録（status なし）は status_of() が pass・skipped などから読み替える

gate を止めないもの（check_ok）: passed と、optional なチェックの not_applicable。
検証（受け入れ条件・リスクに紐づく）は、passed 以外はすべて「確かめていない」。
"""
PASSED, FAILED, ERROR, SKIPPED, NOT_APPLICABLE, PENDING, MANUAL = (
    "passed", "failed", "error", "skipped", "not_applicable", "pending", "manual")
STATUSES = (PASSED, FAILED, ERROR, SKIPPED, NOT_APPLICABLE, PENDING, MANUAL)
LABEL = {PASSED: "合格", FAILED: "不合格", ERROR: "実行エラー", SKIPPED: "未実行", NOT_APPLICABLE: "対象なし",
         PENDING: "保留", MANUAL: "人の確認"}
# 対象のファイルがなければ実行しなくてよいチェックの種類（この run で新しく入った問題がないことが分かる）
OPTIONAL_WHEN_EMPTY = ("lint", "typecheck")
TIMEOUT_EXIT = 124  # util.run_cmd がタイムアウトのときに返す終了コード


def _rec(status, reason=None, **extra):
    r = {"status": status, "pass": status == PASSED}
    if reason:
        r["reason"] = reason
    r.update(extra)
    return r


def status_of(r):
    """記録の status（旧版の記録も読み替える）。"""
    if not r:
        return FAILED
    st = r.get("status")
    if st in STATUSES:
        return st
    if r.get("no_targets"):
        return NOT_APPLICABLE
    if r.get("pending_after_merge"):
        return PENDING
    if r.get("manual"):
        return MANUAL
    if r.get("skipped") or r.get("fail_fast"):
        return SKIPPED
    if r.get("pass"):
        return PASSED
    if r.get("error"):
        return ERROR
    return FAILED


def passed(r):
    return status_of(r) == PASSED


def check_ok(r):
    """このチェックの結果が gate を止めないか。"""
    st = status_of(r)
    return st == PASSED or (st == NOT_APPLICABLE and bool((r or {}).get("optional")))


def reason_of(r):
    r = r or {}
    return r.get("reason") or r.get("skipped") or r.get("error") or (
        f"{LABEL.get(status_of(r), '不合格')}（ログ: {r['log']}）" if r.get("log") else LABEL.get(status_of(r), "不合格"))


# ------------------------------------------------------------------ 記録を作る
def scope(targets):
    return {"mode": "all"} if targets is None else {"mode": "changed", "targets": targets}


def from_run(code, output, sec, log, command, targets=None):
    """コマンドを実行した結果。"""
    if code == TIMEOUT_EXIT and str(output).startswith("タイムアウト"):
        st, why = ERROR, str(output).splitlines()[0]
    elif code == 127:
        st, why = ERROR, "コマンドが見つからない（終了コード 127）"
    elif code == 0:
        st, why = PASSED, None
    else:
        st, why = FAILED, f"終了コード {code}"
    return _rec(st, why, exit=code, sec=sec, log=log, command=command, scope=scope(targets), scoped=targets)


def no_targets(kind, command, required):
    """{changed} の対象が0件で、実行しなかった。required（検証・Lint 以外）なら gate を止める。"""
    optional = (not required) and kind in OPTIONAL_WHEN_EMPTY
    why = "変えたファイルに該当がなく、実行していない" + (
        "" if optional else "（このチェック・検証は対象なしを合格にできない。コマンドの対象を見直す）")
    return _rec(NOT_APPLICABLE, why, optional=optional, command=command, scope=scope(0), scoped=0, sec=0)


def skipped(reason, **extra):
    return _rec(SKIPPED, reason, skipped=reason, **extra)


def pending_after_merge():
    return _rec(PENDING, "マージ後に gate --after-merge で実行する", pending_after_merge=True)


def manual(is_manual):
    if is_manual:
        return _rec(MANUAL, "人の確認が必要（機械では確かめていない）", manual=True)
    return _rec(ERROR, "実行方法（command / checks）が未設定", manual=False, error="実行方法（command / checks）が未設定")


def via_checks(names, check_results):
    """チェックの結果で決まる検証。参照するチェックがすべて実行して合格したときだけ合格。"""
    missing = [n for n in names if n not in check_results]
    not_run = [n for n in names if n in check_results and status_of(check_results[n]) == NOT_APPLICABLE]
    bad = [n for n in names if n in check_results and not passed(check_results[n])]
    if missing:
        st, why = FAILED, f"参照するチェックが未登録: {', '.join(missing)}"
    elif not_run:
        st, why = NOT_APPLICABLE, f"参照するチェックが対象なしで実行されていない: {', '.join(not_run)}"
    elif bad:
        st, why = FAILED, f"参照するチェックが合格していない: {', '.join(bad)}"
    else:
        st, why = PASSED, None
    return _rec(st, why, via_checks=list(names), missing_checks=missing)


def from_holdout(code, sec):
    return _rec(PASSED if code == 0 else FAILED, None if code == 0 else f"終了コード {code}", exit=code, sec=sec)


def diff_coverage(measure, min_pct, report, evidence=None):
    """差分カバレッジの測定値（quality.measure_diff_coverage）から合否を決める。"""
    base = {"min": min_pct, "report": report}
    if evidence:
        base["evidence"] = evidence
    if measure.get("error"):
        return dict(_rec(ERROR, measure["error"], error=measure["error"]), **base)
    total, covered = measure["total"], measure["covered"]
    pct = round(covered * 100.0 / total, 1) if total else None
    out = dict(base, percent=pct, covered=covered, total=total, files=measure["files"],
               uncovered={k: v[:50] for k, v in measure["uncovered"].items()}, unmeasured=measure["unmeasured"],
               approx=measure["approx"])
    if measure["unmeasured"]:
        why = ("変えたファイルがカバレッジのレポートにない（テストで一度も読み込まれていない・計測の対象外）: "
               + ", ".join(measure["unmeasured"][:5]) + "（対象外にするなら quality.diff_coverage.exclude）")
        return dict(_rec(FAILED, why, error=why), **out)
    if total and pct < min_pct:
        why = f"差分カバレッジ {pct}% が基準 {min_pct:g}% 未満（変えた行 {total} 行のうち {covered} 行）"
        return dict(_rec(FAILED, why, error=why), **out)
    if not total:
        return dict(_rec(NOT_APPLICABLE, "変えた行のうち、計測の対象になる行がない", optional=True), **out)
    return dict(_rec(PASSED), **out)


# ------------------------------------------------------------------ 判定
def checks_pass(check_results, has_checks):
    return bool(has_checks) and all(check_ok(r) for r in check_results.values())


def failures(results):
    """gate を止めている項目（修正依頼・問題の表示に使う共通の結果）。"""
    res = results or {}
    checks = [(n, r) for n, r in (res.get("checks") or {}).items() if not check_ok(r)]
    vers = [(v, r) for v, r in (res.get("verifications") or {}).items()
            if status_of(r) not in (PASSED, PENDING, MANUAL)]
    hold = [ac for ac, r in (res.get("holdout") or {}).items() if not passed(r)]
    dc = res.get("diff_coverage")
    dc_bad = dc if dc and status_of(dc) not in (PASSED, NOT_APPLICABLE) else None
    return {"checks": checks, "verifications": vers, "holdout": hold, "diff_coverage": dc_bad}


def overall(results, has_checks):
    """gate 全体の合否（実行中の変更は呼び出し側で別に判定する）。"""
    f = failures(results)
    return checks_pass((results or {}).get("checks") or {}, has_checks) and not f["holdout"] and not f["diff_coverage"]
