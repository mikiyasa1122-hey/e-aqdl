"""差分の品質: この run で変えた部分だけを機械で確かめる（Codacy の「新しい問題だけを止める」を参考にした）。

- 起点（baseline）: run の開始時点のコード（スナップショットと git の HEAD）
- チェックのコマンドの `{changed}` / `{changed:.py,.pyi}`: 起点から変わった・増えたファイルに置き換える
  （例: `ruff check {changed:.py}`）。既存のコードの古い問題で止めず、この run で書いたコードの問題で止める
- 差分カバレッジ（quality.diff_coverage）: 変えた行のうち、テストで実行された行の割合。
  カバレッジのレポート（coverage.py の JSON / LCOV）を読み、基準未満なら gate を不合格にする

評価（A〜F などの成績）は付けない。合否と、どの行がテストで実行されていないかだけを返す。
"""
import difflib
import fnmatch
import os
import re
import shlex
import subprocess
import json

from .util import now_iso, sh_run

CHANGED_RE = re.compile(r"\{changed(?::([^{}]*))?\}")
DEFAULT_CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift",
                    ".rb", ".php", ".cs", ".vue", ".svelte")


def config_of(cfg):
    q = (cfg.get("quality") or {}).get("diff_coverage") or {}
    rep = str(q.get("report") or "").replace("\\", "/")
    while rep.startswith("./"):
        rep = rep[2:]
    return {"enabled": bool(q.get("enabled")), "report": rep, "min": float(q.get("min", 80)),
            "exclude": list(q.get("exclude") or [])}


def report_path(cfg):
    c = config_of(cfg)
    return c["report"] if c["enabled"] and c["report"] else None


# ------------------------------------------------------------------ baseline
def capture_baseline(project, snap):
    head = None
    code, out = sh_run("git rev-parse --verify -q HEAD", project, 30) if os.path.isdir(project) else (1, "")
    if code == 0 and re.fullmatch(r"[0-9a-f]{40,64}", out.strip()):
        head = out.strip()
    return {"snapshot": snap, "git_head": head, "at": now_iso()}


def changed_files(state, snap):
    """起点から変わった・増えたファイル（今あるものだけ）。起点がない（旧版の run）なら None。"""
    base = (state.get("baseline") or {}).get("snapshot")
    if base is None:
        return None
    return sorted(f for f, v in snap.items() if f not in base or base[f][2] != v[2])


# ------------------------------------------------------------------ {changed}
def _quote(paths):
    if os.name == "nt":
        return subprocess.list2cmdline(paths)
    return " ".join(shlex.quote(p) for p in paths)


def _exts(spec):
    return tuple(e.strip() if e.strip().startswith(".") else "." + e.strip() for e in spec.split(",") if e.strip())


def expand(command, changed, all_files):
    """コマンドの {changed[:拡張子]} を置き換える。

    戻り値: (コマンド, 対象の数)。置き換えがなければ (コマンド, None)。
    対象が0件なら (None, 0)（実行しない。変えたファイルに該当がないため）。
    起点がない run（changed が None）は、全ファイルを対象にする（厳しい側に倒す）。
    """
    if not command or not CHANGED_RE.search(command):
        return command, None
    pool = all_files if changed is None else changed
    total = 0

    def rep(m):
        nonlocal total
        ex = _exts(m.group(1) or "")
        files = [f for f in pool if not ex or f.endswith(ex)]
        total += len(files)
        return _quote(files)

    out = CHANGED_RE.sub(rep, command)
    return (out, total) if total else (None, 0)


# ------------------------------------------------------------------ changed lines
def _git_versions(project, head, rel):
    """起点の HEAD にあるファイルの内容の候補。

    作業ツリーと同じ形（改行コードの変換などを適用した形。Windows の core.autocrlf など）を先に、
    リポジトリに保存された形を後に返す。
    """
    out = []
    for cmd in (["git", "cat-file", "--filters", f"{head}:{rel}"], ["git", "show", f"{head}:{rel}"]):
        try:
            p = subprocess.run(cmd, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0:
            out.append(p.stdout)
    return out


def changed_lines(project, state, rel):
    """変えた行の番号（1始まり）。戻り値: (行の集合, 近似か)。

    起点に無いファイルは全行。起点で git の HEAD と同じ内容だったファイルは、HEAD の内容と比べた差分の行。
    起点の内容が取り出せないときは全行（近似。厳しい側）。
    """
    import hashlib
    path = os.path.join(project, rel)
    try:
        with open(path, "rb") as f:
            new = f.read()
    except OSError:
        return set(), False
    new_lines = new.decode("utf-8", "replace").splitlines()
    all_lines = set(range(1, len(new_lines) + 1))
    base = state.get("baseline") or {}
    bsnap = base.get("snapshot") or {}
    if rel not in bsnap:
        return all_lines, False
    cands = _git_versions(project, base["git_head"], rel) if base.get("git_head") else []
    # 起点のスナップショットと同じ内容のものだけを使う（起点で HEAD から変わっていたファイルは使えない）
    old = next((c for c in cands if hashlib.sha1(c).hexdigest() == bsnap[rel][2]), None)
    if old is None:
        return all_lines, True
    sm = difflib.SequenceMatcher(None, old.decode("utf-8", "replace").splitlines(), new_lines, autojunk=False)
    out = set()
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out, False


# ------------------------------------------------------------------ coverage reports
def _norm(project, p):
    p = str(p).replace("\\", "/")
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, project).replace("\\", "/")
        except ValueError:
            return p
    return p[2:] if p.startswith("./") else p


def parse_report(project, path):
    """{相対パス: {"executable": set, "covered": set}}。読めなければ例外。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    out = {}
    if text.lstrip().startswith("{"):
        data = json.loads(text)
        files = data.get("files")
        if not isinstance(files, dict):
            raise ValueError("coverage.py の JSON（files）ではない")
        for p, d in files.items():
            ex = set(d.get("executed_lines") or [])
            miss = set(d.get("missing_lines") or [])
            out[_norm(project, p)] = {"executable": ex | miss, "covered": ex}
        return out
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SF:"):
            cur = out.setdefault(_norm(project, line[3:]), {"executable": set(), "covered": set()})
        elif line.startswith("DA:") and cur is not None:
            parts = line[3:].split(",")
            try:
                n, hits = int(parts[0]), int(float(parts[1]))
            except (ValueError, IndexError):
                continue
            cur["executable"].add(n)
            if hits > 0:
                cur["covered"].add(n)
        elif line == "end_of_record":
            cur = None
    if not out:
        raise ValueError("coverage.py の JSON でも LCOV でもない")
    return out


def _is_test(rel):
    from .gate import TEST_FILE
    return bool(TEST_FILE.search(rel))


def diff_coverage(project, state, cfg, snap, started_at):
    """差分カバレッジ。無効なら None。戻り値は gate の結果に記録する。"""
    c = config_of(cfg)
    if not c["enabled"]:
        return None
    res = {"pass": False, "min": c["min"], "report": c["report"]}
    if not c["report"]:
        res["error"] = "quality.diff_coverage.report（カバレッジのレポートの場所）が未設定"
        return res
    rp = os.path.join(project, c["report"])
    if not os.path.isfile(rp):
        res["error"] = f"カバレッジのレポート {c['report']} がない（gate のチェックで作られていない）"
        return res
    if os.path.getmtime(rp) < started_at - 1:
        res["error"] = f"カバレッジのレポート {c['report']} がこの gate で作られていない（古いレポートは使わない）"
        return res
    try:
        rep = parse_report(project, rp)
    except (OSError, ValueError) as e:
        res["error"] = f"カバレッジのレポートを読めない: {e}"
        return res
    changed = changed_files(state, snap)
    if changed is None:
        res["error"] = "run の開始時点のコードが記録されていない（旧版で始めた run）。差分を決められない"
        return res
    targets = [f for f in changed if f.endswith(DEFAULT_CODE_EXT) and not _is_test(f)
               and not any(fnmatch.fnmatch(f, g) for g in c["exclude"])]
    covered = total = 0
    uncovered, unmeasured, approx = {}, [], []
    for f in targets:
        lines, ap = changed_lines(project, state, f)
        if ap:
            approx.append(f)
        r = rep.get(f)
        if r is None:
            unmeasured.append(f)
            continue
        exe = lines & r["executable"]
        hit = exe & r["covered"]
        covered += len(hit)
        total += len(exe)
        if exe - hit:
            uncovered[f] = sorted(exe - hit)
    pct = round(covered * 100.0 / total, 1) if total else None
    res.update({"percent": pct, "covered": covered, "total": total, "files": len(targets),
                "uncovered": {k: v[:50] for k, v in uncovered.items()}, "unmeasured": unmeasured,
                "approx": approx})
    if unmeasured:
        res["error"] = (f"変えたファイルがカバレッジのレポートにない（テストで一度も読み込まれていない・計測の対象外）: "
                        f"{', '.join(unmeasured[:5])}（対象外にするなら quality.diff_coverage.exclude）")
    elif total and pct < c["min"]:
        res["error"] = f"差分カバレッジ {pct}% が基準 {c['min']:g}% 未満（変えた行 {total} 行のうち {covered} 行）"
    res["pass"] = "error" not in res
    return res


def ranges(nums, limit=8):
    """[1,2,3,7] → '1-3, 7'"""
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out[:limit]) + (" …" if len(out) > limit else "")


def summary_lines(g):
    """レビュー担当・実装担当に渡す、機械の確認の要約（gate の結果から）。"""
    res = (g or {}).get("results") or {}
    lines = []
    scoped = [(n, r) for n, r in (res.get("checks") or {}).items() if r.get("scoped") is not None]
    for n, r in scoped:
        if r.get("no_targets"):
            lines.append(f"- チェック {n}（変えたファイルだけ）: 対象なし（該当する変更がない）")
        else:
            lines.append(f"- チェック {n}（変えたファイル {r['scoped']} 件だけ）: {'合格' if r.get('pass') else '不合格'}")
    dc = res.get("diff_coverage")
    if dc:
        if dc.get("percent") is not None or dc.get("total") == 0:
            pct = "対象の行なし" if not dc.get("total") else f"{dc['percent']}%（{dc['covered']}/{dc['total']} 行）"
            lines.append(f"- 差分カバレッジ: {pct}、基準 {dc['min']:g}% → {'合格' if dc.get('pass') else '不合格'}")
        if dc.get("error"):
            lines.append(f"  - {dc['error']}")
        for f, ls in list((dc.get("uncovered") or {}).items())[:10]:
            lines.append(f"  - テストで実行されていない変更行: {f}: {ranges(ls)}")
        if dc.get("approx"):
            lines.append(f"  - 起点の内容を取り出せず、全行を変更として数えたファイル: {', '.join(dc['approx'][:5])}")
    return lines
