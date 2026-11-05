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
import hashlib
import json
import os
import re
import shlex
import subprocess

from .fileclass import is_code, is_test
from .util import now_iso, sh_run

# 依存の向き: gate → quality → fileclass / util。quality は gate・verdict を import しない
# （合否の判定は verdict.diff_coverage、文章への整形は agents が行う）。

CHANGED_RE = re.compile(r"\{changed(?::([^{}]*))?\}")


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


def invalidate_report(project, cfg):
    """gate の実行の前に、前回のレポートを消す（このあとにあれば、この gate のチェックが作ったもの）。

    戻り値: (消したか, エラーの文字列)。消せなければエラー（安全側に倒し、差分カバレッジを不合格にする）。
    """
    rel = report_path(cfg)
    if not rel:
        return False, None
    p = os.path.join(project, rel)
    if not os.path.lexists(p):
        return False, None
    try:
        os.remove(p)
    except OSError as e:
        return False, f"前回のカバレッジのレポート {rel} を消せない（{e}）。この gate で作られたものか確かめられない"
    return True, None


def measure_diff_coverage(project, state, cfg, snap, invalidation_error=None):
    """差分カバレッジの測定（ファイル・git を読む）。合否は verdict.diff_coverage が決める。

    戻り値: {"total", "covered", "files", "uncovered", "unmeasured", "approx"} か {"error": ...}、と証拠。
    レポートは gate の前に invalidate_report で消してあるので、あればこの gate のチェックが作ったもの。
    """
    c = config_of(cfg)
    if invalidation_error:
        return {"error": invalidation_error}, None
    if not c["report"]:
        return {"error": "quality.diff_coverage.report（カバレッジのレポートの場所）が未設定"}, None
    rp = os.path.join(project, c["report"])
    if not os.path.isfile(rp):
        return {"error": f"カバレッジのレポート {c['report']} がこの gate で作られていない（gate のチェックで作る）"}, None
    try:
        with open(rp, "rb") as fh:
            ev = {"report": c["report"], "sha1": hashlib.sha1(fh.read()).hexdigest()}
        rep = parse_report(project, rp)
    except (OSError, ValueError) as e:
        return {"error": f"カバレッジのレポートを読めない: {e}"}, None
    changed = changed_files(state, snap)
    if changed is None:
        return {"error": "run の開始時点のコードが記録されていない（旧版で始めた run）。差分を決められない"}, ev
    targets = [f for f in changed if is_code(f) and not is_test(f)
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
    return {"total": total, "covered": covered, "files": len(targets), "uncovered": uncovered,
            "unmeasured": unmeasured, "approx": approx}, ev


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
