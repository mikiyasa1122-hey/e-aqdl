"""欠陥検出ベンチマーク（PERF-01）の実行。

  python3 bench/review/run_bench.py build  --out DIR [--cases ID,...]
      各ケースを、エンジンで実際の run としてレビューの直前（gate 合格）まで進め、レビュー担当への
      引き継ぎ情報（エンジンが SubagentStart で返す本物の文面）を保存する
  python3 bench/review/run_bench.py review --out DIR [--parallel 3] [--rep 1]
      レビュー担当（agents/reviewer.md を指示に、引き継ぎ情報を入力に）を claude -p で動かす
  python3 bench/review/run_bench.py judge  --out DIR
      正解を知る判定役（別の claude -p、ツールなし）が、指摘を case_id・原因・箇所で照合する
  python3 bench/review/run_bench.py report --out DIR
      集計（report.md・summary.json）

--set with-sql で、既存の40ケースに SQL の追加ケース（sql_cases.py。EVAL-02）を足す（--set sql で追加ケースだけ）。
--set clean で、クリーンケース群（clean_cases.py。欠陥なし・受け入れ条件ごとのテストあり）を使う（EVAL-01）。
集計は clean_report.py。既存の40ケースとは別の DIR に出し、結果を混ぜない。

レビュー担当には正解（cases.py・repro）を渡さない。ケースの作業フォルダには、参照コードに欠陥を入れた
コードと見えるテストだけがある。レビュー担当は hook なしで動くため、Write / Edit を外し、レビュー後に
ファイルが変わっていないかを確かめる（変わっていたら無効）。
"""
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import cases as CS  # noqa: E402
import check_cases as CC  # noqa: E402
import clean_cases as CL  # noqa: E402
import sql_cases as SQ  # noqa: E402
import run_docs as D  # noqa: E402

REVIEWER_MD = os.path.join(ROOT, "plugins", "aq-dlc", "agents", "reviewer.md")
REVIEW_TASK = "上の引き継ぎ情報に従って独立レビューを行い、最後に指定の形式（## レビュー結果）で報告してください。"


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def selected():
    ids = arg("--cases")
    pool = {"clean": CL.CASES, "sql": SQ.CASES, "with-sql": CS.CASES + SQ.CASES}.get(arg("--set"), CS.CASES)
    return [c for c in pool if not ids or c["id"] in ids.split(",")]


def engine_commit():
    p = subprocess.run(["git", "rev-parse", "HEAD:plugins/aq-dlc"], cwd=ROOT, capture_output=True, text=True)
    h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return {"repo_commit": h.stdout.strip(), "engine_tree": p.stdout.strip()}


# ------------------------------------------------------------------ build
def build_one(case, out):
    import tempfile
    work = os.path.join(out, "work")
    os.makedirs(work, exist_ok=True)
    tempfile.tempdir = work
    from helpers import Env, denied  # noqa: E402（tests/helpers.py。エンジンと hook を本物で動かす）
    e = Env(git=True)
    src = os.path.join(out, "src", case["id"])
    shutil.rmtree(src, ignore_errors=True)
    CC.materialize(case, src)
    p = e.project
    with open(os.path.join(p, "README.md"), "w", encoding="utf-8") as f:
        f.write("# notes\n組織ごとのメモのライブラリ。\n")
    e.git("add", "-A")
    e.git("commit", "-q", "-m", "初期状態")
    e.aq("init", "notes")
    e.aq("new", "チームのメモのライブラリを作る")
    e.aq("memory-scope", "project")
    assert not denied(e.spawn("requirements"))
    e.start("requirements")
    for rid, text, critical, ac, ac_text in D.REQUIREMENTS:
        kind = ["--kind", "nonfunctional"] if rid in ("R9", "R10") else []
        e.as_role("requirements", "req", rid, text, *(["--critical"] if critical else []), *kind)
        e.as_role("requirements", "accept", rid, ac, ac_text)
        e.as_role("requirements", "verify", ac, f"V{ac[2:]}", f"{ac} を単体テストで確かめる", "--level", "unit")
    for rid, kid, text in D.RISKS:
        e.as_role("requirements", "risk", rid, kid, text)
        e.as_role("requirements", "verify", kid, f"V{kid}", f"{kid} を単体テストで確かめる", "--level", "unit")
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), p), D.SPEC_MD)
    r = e.stop("requirements", "要求を整理")
    assert r is None, r
    e.approve_spec("ユーザーが『この仕様で進めて』と返答")
    assert not denied(e.spawn("design"))
    e.start("design")
    s = e.state()
    for rid, *_ in D.REQUIREMENTS:
        e.as_role("design", "design-map", rid, "notes/ パッケージ")
    e.as_role("design", "add-check", "unit", f'"{sys.executable}" -m unittest discover -s tests -t . -q',
              "--kind", "unit")
    e.write_as("design", os.path.relpath(os.path.join(e.arts, "design.md"), p),
               D.DESIGN_MD.format(spec=s["spec"]["registered_hash"][:8]))
    assert e.stop("design", "設計完了") is None
    assert not denied(e.spawn("implementer"))
    e.start("implementer")
    for base, _dirs, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(base, fn), src).replace(os.sep, "/")
            with open(os.path.join(base, fn), encoding="utf-8") as f:
                e.write_as("implementer", rel, f.read())
    files = sorted(os.path.relpath(os.path.join(b, f), src).replace(os.sep, "/")
                   for b, _d, fs in os.walk(os.path.join(src, "notes")) for f in fs)
    for rid, *_ in D.REQUIREMENTS:
        e.as_role("implementer", "impl-map", rid, "--files", *[f"notes/{os.path.basename(x)}" for x in files])
    s = e.state()
    for r in s["requirements"]:
        for part in r.get("acceptance", []) + r.get("risks", []):
            for v in part.get("verifications", []):
                e.as_role("implementer", "bind", v["id"], "--checks", "unit")
    e.write_as("implementer", os.path.relpath(os.path.join(e.arts, "impl.md"), p),
               D.IMPL_MD.format(design=s["design"]["registered_hash"][:8], spec=s["spec"]["registered_hash"][:8]))
    assert e.stop("implementer", "実装完了") is None
    e.aq("gate")
    g = e.state()["gate"]
    if not g["checks_pass"]:
        raise AssertionError(f"{case['id']}: gate が合格しない（ケースが不正）")
    assert not denied(e.spawn("reviewer"))
    brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
    # ブラインドレビュー（既定）では実装報告を読めない（hook が止める）。hook なしで動かすので、作業フォルダから外す
    impl = os.path.join(e.arts, "impl.md")
    shutil.move(impl, os.path.join(out, "src", case["id"] + "-impl.md"))
    meta = {"case": case["id"], "project": p, "home": e.home, "brief": brief,
            "snapshot": snapshot(p), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(os.path.join(out, "built", case["id"] + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return case["id"]


def snapshot(project):
    import hashlib
    out = {}
    for base, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".aq-dlc")]
        for fn in files:
            path = os.path.join(base, fn)
            with open(path, "rb") as f:
                out[os.path.relpath(path, project)] = hashlib.sha1(f.read()).hexdigest()
    return out


# ------------------------------------------------------------------ review
def reviewer_prompt():
    with open(REVIEWER_MD, encoding="utf-8") as f:
        text = f.read()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def review_one(meta, out, rep):
    rid = f"{meta['case']}#{rep}"
    dest = os.path.join(out, "reviews", f"{meta['case']}.{rep}.json")
    if os.path.exists(dest):
        return rid, "skip"
    t0 = time.time()
    cmd = ["claude", "-p", meta["brief"] + "\n\n" + REVIEW_TASK, "--append-system-prompt", reviewer_prompt(),
           "--tools", "Read,Grep,Glob,Bash", "--dangerously-skip-permissions", "--output-format", "json"]
    # 引き継ぎ情報のエンジンのコマンド（trace・history）が、この run の状態を読めるようにする
    env = dict({k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}, AQDLC_HOME=meta["home"])
    p = subprocess.run(cmd, cwd=meta["project"], capture_output=True, text=True, env=env, timeout=1800)
    try:
        res = json.loads(p.stdout)
    except ValueError:
        res = {"is_error": True, "result": (p.stdout + p.stderr)[-2000:]}
    changed = sorted(k for k, v in snapshot(meta["project"]).items() if meta["snapshot"].get(k) != v) + sorted(
        k for k in meta["snapshot"] if k not in snapshot(meta["project"]))
    rec = {"case": meta["case"], "rep": rep, "sec": round(time.time() - t0, 1), "exit": p.returncode,
           "cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"),
           "models": sorted((res.get("modelUsage") or {}).keys()), "usage": res.get("usage"),
           "is_error": bool(res.get("is_error")), "result": res.get("result"), "files_changed": changed,
           "valid": not res.get("is_error") and p.returncode == 0 and not changed}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rid, "ok" if rec["valid"] else "invalid"


# ------------------------------------------------------------------ judge
JUDGE = """あなたは欠陥検出ベンチマークの判定役です。レビュー担当の報告の各指摘を、正解データとコードに照らして分類します。
推測で甘くしないこと。日本語で答え、最後に JSON だけを ```json のブロックで出力してください。

## ケース
- case_id: {case_id}
- 埋め込んだ欠陥: {truth}

## 仕様書（レビュー担当が見たもの）
{spec}

## このケースのコード（レビュー担当が見たもの）
{code}
{tests}
## レビュー担当の報告
{review}

## 分類の規則
各指摘（[重大n]・[軽微n]）を1つずつ、次のどれかに分類する:
- matches_defect: 埋め込んだ欠陥そのもの（同じ箇所で、原因か影響が一致）を指摘している
- real_other: 埋め込んだ欠陥ではないが、コードの実在する欠陥（仕様に反する・バグ・セキュリティの穴）を、根拠を挙げて指摘している
- test_gap: コードの欠陥ではなく、テストの不足・弱さだけを指摘している
- false_positive: 欠陥でないものを欠陥と指摘している（仕様・コードを読み違えている）
- style: 好み・スタイル・保守性の提案で、欠陥の主張ではない
- ambiguous: 上のどれか判断できない（人が確かめる）
欠陥なしのケースでは matches_defect は使わない。
{test_note}
defect_detected は、埋め込んだ欠陥があるケースで、どれかの指摘が matches_defect なら "yes"、欠陥の箇所に触れているが原因・影響が違う・曖昧なら "partial"、触れていなければ "no"。欠陥なしのケースでは "n/a"。

```json
{{"findings": [{{"label": "重大1", "class": "matches_defect|real_other|test_gap|false_positive|style|ambiguous", "reason": "1文"}}],
  "defect_detected": "yes|partial|no|n/a", "matched_label": "重大1 または null", "note": "1文"}}
```
"""


def case_code(case_id, out):
    src = os.path.join(out, "src", case_id)
    parts = []
    for rel in ("notes/service.py", "notes/ratelimit.py", "notes/share.py", "notes/db.py"):
        with open(os.path.join(src, rel), encoding="utf-8") as f:
            lines = f.read().splitlines()
        parts.append(f"### {rel}\n```python\n" + "\n".join(f"{i + 1:4d} {ln}" for i, ln in enumerate(lines)) + "\n```")
    return "\n\n".join(parts)


def case_tests(case_id, out):
    """テストのファイル（クリーンケース群だけ判定役に見せる。テスト不足の指摘が正しいかを確かめるため）。"""
    d = os.path.join(out, "src", case_id, "tests")
    parts = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".py") and fn != "__init__.py":
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                parts.append(f"### tests/{fn}\n```python\n{f.read()}\n```")
    return "\n## このケースのテスト（レビュー担当が見たもの）\n" + "\n\n".join(parts) + "\n"


CLEAN_TEST_NOTE = """このケースは、受け入れ条件 AC1〜AC10 とリスク K1〜K8 ごとのテストを揃えた、欠陥のない題材として作った。
ただし作り手の見落としはありうるので、正解を鵜呑みにせず、コードとテストで確かめる。
- テスト不足の指摘は、上のテストに実際に不足がある（その受け入れ条件・リスクを確かめるテストがない・空洞）ときだけ test_gap。
  テストがあるのに「ない」と言っている、または必須でない追加を必須と言っているなら false_positive。
- コードの欠陥の指摘は、実在するときだけ real_other（その場合は reason に、成立する入力・状態を書く）。
"""


def judge_one(rev_path, out):
    with open(rev_path, encoding="utf-8") as f:
        rev = json.load(f)
    dest = rev_path.replace(os.sep + "reviews" + os.sep, os.sep + "judged" + os.sep)
    if os.path.exists(dest) or not rev["valid"]:
        return rev["case"], "skip"
    c = next(x for x in CC.all_cases() if x["id"] == rev["case"])
    clean = bool(c.get("clean_set"))
    truth = ("欠陥なし（振る舞いを変えない変更: " + c["title"] + "）") if not c["defect"] else (
        f"{c['title']}（分類 {c['category']}・重大度 {c['severity']}・箇所 {c['location']}・原因: {c['cause']}・影響: {c['impact']}）")
    prompt = JUDGE.format(case_id=c["id"], truth=truth, spec=D.SPEC_MD, code=case_code(c["id"], out), review=rev["result"],
                          tests=case_tests(c["id"], out) if clean else "", test_note=CLEAN_TEST_NOTE if clean else "")
    p = subprocess.run(["claude", "-p", prompt, "--tools", "", "--output-format", "json"], capture_output=True,
                       text=True, timeout=900, cwd=out)
    try:
        res = json.loads(p.stdout)
    except ValueError:
        return rev["case"], "judge_error"
    m = re.findall(r"```json\s*(\{.*?\})\s*```", res.get("result") or "", re.S)
    parsed = json.loads(m[-1]) if m else None
    if res.get("is_error") or parsed is None:
        # 利用上限・判定役の失敗は保存しない（次の judge でやり直す。判定済みとして扱わない）
        return rev["case"], "judge_error: " + (res.get("result") or "")[:80]
    rec = {"case": rev["case"], "rep": rev["rep"], "judge_cost_usd": res.get("total_cost_usd"),
           "judge_raw": res.get("result"), "parsed": parsed}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rev["case"], "ok" if parsed else "parse_error"


# ------------------------------------------------------------------ main
def main():
    cmd, out = sys.argv[1], os.path.abspath(arg("--out", os.path.join(HERE, "results", "run")))
    for d in ("built", "reviews", "judged", "src"):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    if cmd == "build":
        with open(os.path.join(out, "conditions.json"), "w", encoding="utf-8") as f:
            json.dump(dict(engine_commit(), case_set=arg("--set", "default"), python=sys.version.split()[0], built=time.strftime("%Y-%m-%dT%H:%M:%S"),
                           config="既定（init のまま）。counterfactual off・holdout 無効・差分カバレッジ無効・"
                                  "review.blind 有効・workflow.mode full"), f, ensure_ascii=False, indent=1)
        for c in selected():
            print("built", build_one(c, out), flush=True)
    elif cmd == "review":
        metas = []
        for c in selected():
            with open(os.path.join(out, "built", c["id"] + ".json"), encoding="utf-8") as f:
                metas.append(json.load(f))
        reps = range(1, int(arg("--rep", "1")) + 1)
        with cf.ThreadPoolExecutor(int(arg("--parallel", "3"))) as ex:
            for rid, st in ex.map(lambda mr: review_one(mr[0], out, mr[1]), [(m, r) for r in reps for m in metas]):
                print(rid, st, flush=True)
    elif cmd == "judge":
        paths = sorted(os.path.join(out, "reviews", f) for f in os.listdir(os.path.join(out, "reviews")))
        with cf.ThreadPoolExecutor(int(arg("--parallel", "4"))) as ex:
            for cid, st in ex.map(lambda pth: judge_one(pth, out), paths):
                print(cid, st, flush=True)
    elif cmd == "report":
        if arg("--set") == "clean":
            import clean_report as CR
            CR.write(out)
        else:
            import report as RP
            RP.write(out)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
