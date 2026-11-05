"""隠し受け入れテスト（holdout）。

設計担当が受け入れ条件ごとのテストを、プロジェクトの外（~/.aq-dlc/holdout/<プロジェクト>/<run>/）に置く。
実装担当には「どの受け入れ条件が holdout で確かめられるか」だけを伝え、中身は読ませない（Read・Grep・Glob と
Bash を hook で止める）。gate で初めて実行し、結果は合否と受け入れ条件の ID だけを返す（出力は holdout の
置き場所のログに残し、プロジェクトには書かない）。

限界: 同じマシン・同じ利用者で動くため、パスを組み立てて読むなど、意図的に回り込めば読める。悪意のない
エージェントがうっかり中身に合わせて実装する（テストに実装を合わせる）ことを防ぐ仕組みであり、隔離ではない。
"""
import hashlib
import os
import time

from . import gate as G
from . import verdict as V
from .project import home
from .util import now_iso


def config_of(cfg):
    h = cfg.get("holdout") or {}
    return {"enabled": bool(h.get("enabled")), "require_for_critical": bool(h.get("require_for_critical", True))}


def enabled(cfg):
    return config_of(cfg)["enabled"]


def root():
    return os.path.join(home(), "holdout")


def run_holdout_dir(project, run_id):
    key = hashlib.sha1(os.path.realpath(project).encode("utf-8")).hexdigest()[:16]
    return os.path.join(root(), key, str(run_id))


def items(state):
    return (state.get("holdout") or {}).get("items", [])


def files_of(d):
    out = []
    if not os.path.isdir(d):
        return out
    for base, dirs, fs in os.walk(d):
        dirs[:] = [x for x in dirs if x not in ("logs", "__pycache__", ".pytest_cache")]
        for f in fs:
            out.append(os.path.relpath(os.path.join(base, f), d).replace(os.sep, "/"))
    return sorted(out)


def fingerprint(project, state):
    """holdout の定義（コマンドと置き場所のファイルの内容）の指紋。gate の結果の鮮度の判定に使う。"""
    if not items(state):
        return None
    d = run_holdout_dir(project, state.get("run_id"))
    h = hashlib.sha1()
    for it in sorted(items(state), key=lambda x: x["ac"]):
        h.update(f"{it['ac']}\0{it['command']}\0".encode("utf-8"))
    for f in files_of(d):
        h.update(f.encode("utf-8") + b"\0")
        try:
            with open(os.path.join(d, f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


def expand(command, d):
    return command.replace("{holdout}", d)


def run_all(project, state, cfg, stamp):
    """holdout を実行する。戻り値: {AC: {"pass", "exit", "sec"}}（出力は holdout の置き場所のログにだけ残す）。"""
    d = run_holdout_dir(project, state.get("run_id"))
    log_dir = os.path.join(d, "logs")
    os.makedirs(log_dir, exist_ok=True)
    env = G.gate_env(cfg)
    env = dict(env, AQDLC_HOLDOUT_DIR=d)
    default_to = int(cfg["gate"]["default_timeout_sec"])
    out = {}
    for it in items(state):
        t0 = time.time()
        code, text, _dur = G.run_cmd(expand(it["command"], d), project, default_to, env)
        with open(os.path.join(log_dir, f"{stamp}-{it['ac']}.log"), "w", encoding="utf-8") as f:
            f.write(f"$ {it['command']}\nexit={code}\n\n{text}")
        out[it["ac"]] = V.from_holdout(code, round(time.time() - t0, 2))
        print(f"  {'合格' if code == 0 else '不合格'} 隠しテスト:{it['ac']}")
    return out


def coverage_problems(state, cfg, project):
    """設計の終了時に確かめること（有効なときだけ）。"""
    if not enabled(cfg):
        return []
    errs = []
    d = run_holdout_dir(project, state.get("run_id"))
    reg = {it["ac"] for it in items(state)}
    if not reg:
        errs.append("隠し受け入れテスト（holdout）が1つも登録されていない（`holdout <受け入れ条件ID> --command ...`）")
    if reg and not files_of(d):
        errs.append(f"holdout のテストファイルが置き場所（{d}）にない")
    if config_of(cfg)["require_for_critical"]:
        for r in state.get("requirements", []):
            if r.get("critical") and r.get("acceptance") and not any(a["id"] in reg for a in r["acceptance"]):
                errs.append(f"重要な要求 {r['id']} の受け入れ条件に holdout がない")
    return errs


def record(state, ac, command):
    h = state.setdefault("holdout", {"items": []})
    h["items"] = [x for x in h.get("items", []) if x["ac"] != ac] + [{"ac": ac, "command": command, "at": now_iso()}]


def remove(state, ac):
    h = state.setdefault("holdout", {"items": []})
    before = len(h.get("items", []))
    h["items"] = [x for x in h.get("items", []) if x["ac"] != ac]
    return before != len(h["items"])
