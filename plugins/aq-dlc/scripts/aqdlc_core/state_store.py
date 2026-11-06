"""run の状態の保存場所・読み込み・保存（ロック・整合性の検証・監査ログ）・移行・新しい run・指紋の計算（Ctx）。
ファイルの読み書きなど、副作用のある処理はここに集める。判定は state_policy にある。

state.py から本文を変えずに移した。"""
import datetime as _dt
import os
import time

from . import gate as G
from .decision_sheet import fingerprint_items as _sheet_fp_items
from .project import config, register_active
from .util import (FileLock, die, fingerprint, load_json, now_iso, read_text, save_json, sha1_text,
                   slugify)
from .state_model import EAQDL, SCHEMA, bump, graph_fp
from .state_policy import validate_state


# ------------------------------------------------------------------ paths
def runs_dir(project):
    return os.path.join(project, ".aq-dlc", "runs")


def run_dir(project, run_id=None):
    if not run_id:
        run_id = (read_text(os.path.join(runs_dir(project), "CURRENT"), "") or "").strip()
    if not run_id:
        return None
    d = os.path.join(runs_dir(project), run_id)
    return d if os.path.isdir(d) else None


def artifacts_dir(rd):
    return os.path.join(rd, "artifacts")


def doc_path(rd, kind):
    return os.path.join(artifacts_dir(rd), {"spec": "spec.md", "design": "design.md", "impl": "impl.md"}[kind])


def doc_hash(rd, kind):
    t = read_text(doc_path(rd, kind))
    return sha1_text(t) if t is not None else None


# ------------------------------------------------------------------ state io
def empty_state(rid, title):
    return {
        "schema_version": SCHEMA, "run_id": rid, "title": title, "status": "in_progress",
        "created": now_iso(), "seq": 0,
        "awaiting_human": False, "question": "", "asked_seq": 0, "waiting": None,
        "agents": {"running": {}, "history": []},
        "requirements": [], "assumptions": [], "out_of_scope": [], "questions": [],
        "decisions": [], "escalations": [],
        "spec": {}, "design": {"map": {}}, "impl": {"rounds": [], "map": {}},
        "gate": None, "reviews": [], "fix_iterations": 0,
        "human_events": [], "history": [],
        "memory_done": False, "ci": None, "after_merge_gate": False, "stop_blocks": 0,
        "eaqdl": EAQDL, "approvals": [], "rev": 0,
    }


def migrate(state):
    if "eaqdl" not in state:
        state["eaqdl"] = 0  # AQ-DLC v0.5 以前の run（読み取りの互換を保つ）
    base = empty_state(state.get("run_id", "?"), state.get("title", ""))
    for k, v in base.items():
        state.setdefault(k, v)
    state["design"].setdefault("map", {})
    state["impl"].setdefault("rounds", [])
    state["impl"].setdefault("map", {})
    state["agents"].setdefault("running", {})
    state["agents"].setdefault("history", [])
    for r in state["requirements"]:
        r.setdefault("acceptance", [])
        r.setdefault("risks", [])
        r.setdefault("kind", "functional")
    if state.get("schema_version", 1) < SCHEMA:
        state["legacy"] = True
        state["schema_version"] = SCHEMA
    return state


def load_state(project):
    d = run_dir(project)
    if not d:
        return None, None
    p = os.path.join(d, "state.json")
    s = load_json(p, None)
    if not isinstance(s, dict) or not s.get("run_id"):
        # CURRENT が指す run の状態を読めない（削除・空・形式違い）。読めないまま許可しないよう異常として扱う
        raise StateError(f"run の状態（{p}）を読めません（削除・空・形式の違い）。人間に確認してください。"
                         "やり直す場合は new --force で新しい run を始めます（このコマンドと status・doctor・audit は"
                         "エージェントからも実行できます。ほかの操作は止まります）。")
    s = migrate(s)
    s["_rev"] = int(s.get("rev", 0) or 0)
    return s, p


class StateError(Exception):
    """state.json の保存を拒否した（整合性の違反・同時更新）。"""


def save_state(spath, state):
    """検証してから原子的に保存する。

    - 保存しようとする内容の整合性を確かめ、新たな違反を持ち込む保存は拒否する
      （既に壊れていた箇所は problems の state_invalid で人間に知らせる）
    - 読み込んだ後に別のプロセスが保存していたら（rev の不一致）、上書きせずに拒否する
    """
    errs = validate_state(state)
    with FileLock(spath):
        disk = load_json(spath, None) if os.path.exists(spath) else None
        if errs:
            old = set(validate_state(migrate(dict(disk)))) if disk else set()
            new = [e for e in errs if e not in old]
            if new:
                _audit_state(spath, "state.reject", {"errors": new[:10]})
                raise StateError("状態の整合性に反する保存を拒否しました: " + "、".join(new[:5]))
        disk_rev = int((disk or {}).get("rev", 0) or 0)
        if disk is not None and "_rev" in state and state["_rev"] != disk_rev:
            _audit_state(spath, "state.conflict", {"loaded_rev": state["_rev"], "disk_rev": disk_rev})
            raise StateError("state.json が読み込み後に別の処理で更新されました（同時更新）。もう一度実行してください。")
        state["rev"] = disk_rev + 1
        save_json(spath, {k: v for k, v in state.items() if not k.startswith("_")})
        state["_rev"] = state["rev"]


def _audit_state(spath, event, detail):
    from .approval import audit_append
    audit_append(os.path.join(os.path.dirname(spath), "audit.jsonl"), event, **detail)


def require_state(project):
    s, p = load_state(project)
    if not s:
        die("実行中のrunがありません。先に new を実行してください。")
    return s, p


def actor_hint(project):
    """PreToolUse hook が書いた「直前にエンジンを呼んだ役割」。"""
    rd = run_dir(project)
    if not rd:
        return "orchestrator"
    a = load_json(os.path.join(rd, "actor.json"), {}) or {}
    if time.time() - a.get("at", 0) < 60:
        return a.get("role", "orchestrator")
    return "orchestrator"


def log(state, project, action, detail):
    state["history"].append({"seq": bump(state), "at": now_iso(), "actor": actor_hint(project),
                             "action": action, "detail": detail})


def new_run(project, title):
    """run を作成する。ID は秒まで含め、衝突したら連番を付ける（既存の run を上書きしない）。"""
    base = _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + (slugify(title) or "run")
    os.makedirs(runs_dir(project), exist_ok=True)
    rid, n = base, 1
    while True:
        d = os.path.join(runs_dir(project), rid)
        try:
            os.makedirs(d)
            break
        except FileExistsError:
            n += 1
            rid = f"{base}-{n}"
    os.makedirs(os.path.join(d, "gate"), exist_ok=True)
    os.makedirs(artifacts_dir(d), exist_ok=True)
    st = empty_state(rid, title)
    cfg = config(project)
    st["config_fp_registered"] = G.config_fp(cfg, project)
    # 差分の品質の起点（この run で変えた部分を決める）
    from . import quality as Q
    st["baseline"] = Q.capture_baseline(project, G.snapshot(project, cfg, fresh=True))
    default = (cfg.get("memory_policy") or {}).get("scope_default", "ask")
    if default in ("none", "project", "all"):
        from .project import explicit_setting
        # 人が設定に書いた値は "config"（エージェントは変えられない）。既定値のままなら "default"（memory-scope で変えられる）
        src = "config" if explicit_setting(project, "memory_policy", "scope_default") is not None else "default"
        st["memory_scope"] = {"choice": default, "source": src, "at": now_iso(), "seq": 0}
    save_state(os.path.join(d, "state.json"), st)
    tmpl = read_text(os.path.join(project, ".aq-dlc", "run-template.md"), "# {title}\n")
    with open(os.path.join(d, "run.md"), "w", encoding="utf-8") as f:
        f.write(tmpl.replace("{title}", title).replace("{run_id}", rid).replace("{date}", now_iso()))
    with open(os.path.join(runs_dir(project), "CURRENT"), "w", encoding="utf-8") as f:
        f.write(rid + "\n")
    register_active(project, rid)
    return rid, d


# ------------------------------------------------------------------ fingerprints
def spec_hash(state, rd):
    return fingerprint({
        "doc": doc_hash(rd, "spec"),
        "req": [[r["id"], r.get("text"), r.get("kind"), bool(r.get("critical")),
                 [[a["id"], a.get("text")] for a in r.get("acceptance", [])]]
                for r in state.get("requirements", [])],
        "assume": [[a["id"], a.get("text")] for a in state.get("assumptions", [])],
        "out": [[o["id"], o.get("text")] for o in state.get("out_of_scope", [])],
        # 判断シートで決める（決めた）質問と推奨。提示の後に推奨を変えたら、提示し直さないと承認できない。
        # 該当がなければ鍵ごと入れない（判断シートを使わない run の指紋を変えないため）
        **({"sheet": items} if (items := _sheet_fp_items(state.get("questions", []))) else {}),
    })


class Ctx:
    """現在の仕様・設計・コード・検証条件の指紋をまとめて計算する。"""

    def __init__(self, project, state, cfg=None, fresh=False):
        self.project, self.state = project, state
        self.fresh = fresh  # True: キャッシュを使わずに全ファイルをハッシュし直す（判定・登録の時点）
        self.cfg = cfg or config(project)
        self.config_fp = G.config_fp(self.cfg, self.project)
        self.rd = run_dir(project, state.get("run_id"))
        self.spec_hash = spec_hash(state, self.rd)
        self.design_hash = doc_hash(self.rd, "design")
        self.graph_fp = graph_fp(state)
        self.checks = self.cfg.get("checks", [])
        self.checks_fp = G.checks_fp(self.cfg)
        self._snap = None

    @property
    def snap(self):
        if self._snap is None:
            prev = (self.state.get("gate") or {}).get("snapshot")
            self._snap = G.snapshot(self.project, self.cfg, prev, fresh=self.fresh)
        return self._snap

    @property
    def code_fp(self):
        return G.code_fp(self.snap)

    # ---- 判定（state_policy）に渡す事実。判定の側はファイル・git に触れない
    def file_exists(self, rel):
        return os.path.exists(os.path.join(self.project, rel))

    def holdout_fp(self):
        """隠し受け入れテストの定義と置き場所のファイルの指紋（holdout の置き場所を読む）。"""
        from . import holdout as HO
        return HO.fingerprint(self.project, self.state)

    def reviewer_role_fp(self):
        """今のレビュー担当の Role Profile の指紋（Role Profile のファイルを読む）。"""
        from .roles import select_for
        return select_for("reviewer", self.state, self.cfg)["fp"]

    def cf_stale_reasons(self, rec):
        """反証テストの記録が古くなった理由（変異の計画のためにソースを読む）。"""
        from . import counterfactual as CF
        return CF.stale_reasons(self, rec)

    def ci_head(self):
        """CI を確かめる対象のコミット（GitHub Actions の設定とリモートがあるとき）。なければ None。"""
        from .util import sh_run
        if not (os.path.isdir(os.path.join(self.project, ".github", "workflows"))
                and sh_run("git remote get-url origin", self.project, 10)[0] == 0):
            return None
        return sh_run("git rev-parse HEAD", self.project, 10)[1].strip()
