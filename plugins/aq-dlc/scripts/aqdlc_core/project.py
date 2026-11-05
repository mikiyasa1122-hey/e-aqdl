"""全体設定・プロジェクトの初期化・アクティブなrunの登録。"""
import copy
import os
import shutil
import sys
import time

from .util import (deep_merge, die, info, load_json, norm, now_iso, pop_opt, read_text, save_json,
                   slugify, is_within)

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(PKG_DIR)
PLUGIN_ROOT = os.path.dirname(SCRIPTS_DIR)
ENGINE = os.path.join(SCRIPTS_DIR, "aqdlc.py")
TEMPLATES = os.path.join(PLUGIN_ROOT, "templates")


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("AQDLC_HOME", "~/.aq-dlc")))


def active_path():
    return os.path.join(home(), "active.json")


def engine_cmd():
    return f'python3 "{ENGINE}"'


def plugin_version():
    return (load_json(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), {}) or {}).get("version")


DEFAULTS = {
    "workspace_dir": "",
    "github": {"default_visibility": "private", "default_branch": "main"},
    "checks": [],
    "gate": {
        "default_timeout_sec": 600,
        "env": {},
        # プロジェクト直下のビルド出力など（git で追跡しているファイルは除外しない）
        "ignore_dirs": ["dist", "build", "target", ".next", ".turbo", ".vite", "coverage", "out",
                        "test-results", "playwright-report", "blob-report", ".nyc_output",
                        "htmlcov", ".cache", ".parcel-cache", ".svelte-kit"],
    },
    "automation": {
        "auto_start": True,
        "spec_review": True,
        "enforce_roles": True,
        "orchestrator_edits_code": False,
        "force_foreground_agents": True,
        "max_agent_stop_blocks": 3,
        "max_auto_continues": 30,
        "max_total_continues": 200,
        "max_fix_iterations": 8,
        "allow_github_ops": True,
        "block_force_push": True,
        "block_push_to_default_branch": True,
        "block_pr_merge": True,
        "block_destructive_gh": True,
    },
    # --- E-AQDL で追加した設定（既定値のままなら設定の指紋に含めない: 既存の run の互換のため）
    "roles": {"enabled": True, "max_domain_profiles": 3, "disabled": []},
    "counterfactual": {"mode": "off", "timeout_sec": 300, "max_mutations": 12, "per_verification": 2,
                       "auto_plan": True, "copy_extra": []},
    "memory_policy": {
        # run の開始時の Memory の利用範囲: ask（人間に聞く）/ none / project / all
        # 既定は project（プロジェクト内だけ。共通 Memory は使わない）。run ごとに聞くなら ask
        "scope_default": "project",
        # 終了時に共通 Memory へ共有するときは、候補ごとに人間の承認が必要（変更できない）
    },
    # リスクに応じた工程の選択: full（既定。従来どおり全工程）/ auto（要求の文と対象ファイルから選ぶ）/ lite / standard / strict
    "workflow": {"mode": "full"},
    # 独立レビューの独立性
    "review": {
        # ブラインドレビュー: 実装報告（impl.md）と実装担当のメッセージを渡さず、読ませない
        # （実装担当の説明に引きずられないため。仕様・設計・コード・テスト・gate の結果で判断する）
        "blind": True,
        # レビュー担当だけ別のモデルにする（例: "opus"）。空なら統括と同じ。Agent の呼び出しにエンジンが指定する
        "model": "",
    },
    # 隠し受け入れテスト（holdout）: 設計担当が受け入れ条件ごとのテストを、実装担当から読めない場所に置き、
    # gate で初めて実行する。既定は無効（効果を測ってから既定を変える）
    "holdout": {
        "enabled": False,
        # 有効なとき、重要な要求の受け入れ条件には holdout を必須にする
        "require_for_critical": True,
    },
    # 低コスト化（既定はすべて無効＝従来と同じ動き。効果を測ってから有効にする）
    "cost_optimization": {
        # 引き継ぎ情報から、エージェント定義と重複する一般的な説明と、追加の専門性の Role Profile の
        # 責務・成果物・引き継ぎ条件を省く（run 固有の情報・判断基準・禁止事項・検証条件は省かない）
        "compact_handoff": False,
        # gate で、前のチェックが不合格なら、後ろの重いチェック（integration・e2e）を実行しない
        # （実行しなかったものは「未実行（前のチェックの不合格）」で、合格にはしない。gate は不合格のまま）
        "gate_fail_fast": False,
    },
    # 差分の品質（Codacy を参考に、この run で変えた部分だけを機械で確かめる）
    # チェックのコマンドの {changed} / {changed:.py} は設定なしで使える（変えたファイルに置き換わる）
    "quality": {
        # 差分カバレッジ: 変えた行のうち、テストで実行された行の割合。既定は無効（レポートの場所が必要）
        # report: gate のチェックで作られるカバレッジのレポート（coverage.py の JSON か LCOV。例: coverage/coverage.json）
        # exclude: 対象外にするファイルの glob（例: "scripts/*"）。テストファイルは最初から対象外
        "diff_coverage": {"enabled": False, "report": "", "min": 80, "exclude": []},
    },
    "memory": {
        "max_loaded_per_run": 10,
        "auto_save_min_score": 3,
        "promote_to_active_min_runs": 2,
        "max_active_per_type": 30,
    },
}


def ensure_home(verbose=False):
    created = []
    h = home()
    os.makedirs(h, exist_ok=True)
    cfg_path = os.path.join(h, "config.json")
    if not os.path.exists(cfg_path):
        save_json(cfg_path, {
            "_note": "E-AQDL の全体設定。プロジェクトごとの設定は <project>/.aq-dlc/config.json で上書きできる。",
            "workspace_dir": "",
            "github": DEFAULTS["github"],
            "automation": DEFAULTS["automation"],
            "memory": DEFAULTS["memory"],
        })
        created.append(cfg_path)
    src = os.path.join(TEMPLATES, "home", "memory")
    dst = os.path.join(h, "memory")
    for dirpath, _d, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for name in files:
            t = os.path.join(dst, rel, name)
            if not os.path.exists(t):
                shutil.copy2(os.path.join(dirpath, name), t)
                created.append(t)
    if verbose:
        info(f"home: {h}")
        for c in created:
            print(f"  + {c}")
        if not created:
            print("  （既に初期化済み）")


EAQDL_SECTIONS = ("roles", "counterfactual", "memory_policy", "cost_optimization", "review", "holdout", "workflow",
                  "quality")


def config(project=None):
    # 既定値の辞書を呼び出し側の変更から守るため、毎回複製する
    cfg = deep_merge(copy.deepcopy(DEFAULTS), load_json(os.path.join(home(), "config.json"), {}) or {})
    if project:
        cfg = deep_merge(cfg, load_json(os.path.join(project, ".aq-dlc", "config.json"), {}) or {})
    return cfg


def explicit_setting(project, section, key):
    """全体設定・プロジェクト設定のどちらかに、人が明示的に書いた値があれば返す（既定値なら None）。"""
    val = None
    for path in ([os.path.join(home(), "config.json")]
                 + ([os.path.join(project, ".aq-dlc", "config.json")] if project else [])):
        sec = (load_json(path, {}) or {}).get(section)
        if isinstance(sec, dict) and key in sec:
            val = sec[key]
    return val


def project_config_path(project):
    return os.path.join(project, ".aq-dlc", "config.json")


# ------------------------------------------------------------------ project discovery
def find_project(start):
    d = os.path.abspath(os.path.expanduser(start))
    while True:
        if os.path.isfile(os.path.join(d, ".aq-dlc", "project.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_project(args, required=True):
    p = pop_opt(args, "--project") or os.environ.get("AQDLC_PROJECT")
    if p:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(os.path.join(p, ".aq-dlc", "project.json")):
            die(f"E-AQDL プロジェクトではありません: {p}（init で初期化してください）")
        return p
    p = find_project(os.getcwd())
    if p or not required:
        return p
    die("プロジェクトが見つかりません。--project <パス> を付けるか、プロジェクト内で実行してください。")


def register_active(project, run_id):
    a = load_json(active_path(), {}) or {}
    a.setdefault("projects", {})[project] = {"run_id": run_id, "updated": time.time()}
    save_json(active_path(), a)


def unregister_active(project):
    a = load_json(active_path(), {}) or {}
    if project in a.get("projects", {}):
        del a["projects"][project]
        save_json(active_path(), a)


def active_project_for(cwd):
    """セッションのフォルダに関係する、最近更新されたプロジェクト。"""
    if not cwd:
        return None
    p = find_project(cwd)
    if p:
        return p
    a = load_json(active_path(), {}) or {}
    best, best_t = None, -1
    for proj, inf in a.get("projects", {}).items():
        if os.path.isdir(proj) and (is_within(proj, cwd) or is_within(cwd, proj)) \
                and inf.get("updated", 0) > best_t:
            best, best_t = proj, inf.get("updated", 0)
    return best


# ------------------------------------------------------------------ init / create
GITIGNORE_MARK = "# --- AQ-DLC 標準 ---"


def ensure_gitignore(project):
    """AQ-DLC 標準の除外設定を1回だけ追記する（利用者の行は残す）。"""
    path = os.path.join(project, ".gitignore")
    block = read_text(os.path.join(TEMPLATES, "project", ".gitignore"), "")
    cur = read_text(path, "") or ""
    if GITIGNORE_MARK in cur:
        return False
    with open(path, "a", encoding="utf-8") as f:
        if cur and not cur.endswith("\n"):
            f.write("\n")
        f.write(("\n" if cur else "") + block)
    return True


def scaffold(target, name):
    src = os.path.join(TEMPLATES, "project")
    added, kept = [], []
    for dirpath, _d, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(target, rel), exist_ok=True)
        for f in files:
            dst = os.path.normpath(os.path.join(target, rel, f))
            if os.path.exists(dst):
                kept.append(os.path.relpath(dst, target))
                continue
            text = read_text(os.path.join(dirpath, f), "")
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(text.replace("{app_name}", name))
            added.append(os.path.relpath(dst, target))
    save_json(os.path.join(target, ".aq-dlc", "project.json"), {
        "name": name, "slug": slugify(name) or "app", "created": now_iso(),
        "aqdlc_version": plugin_version(),
    })
    return added, kept


def cmd_setup(args):
    ensure_home(verbose=True)


def cmd_init(args):
    ensure_home()
    target = pop_opt(args, "--project") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(target):
        die(f"フォルダがありません: {target}")
    if norm(target) in (norm("~"), norm(os.sep)):
        die("ホームフォルダやルートは初期化できません。アプリ用のフォルダを開いてください。")
    existing = find_project(target)
    if existing and norm(existing) == norm(target):
        info(f"初期化済み: {target}")
        print(target)
        return
    if existing:
        die(f"既存のE-AQDLプロジェクトの内側です: {existing}")
    name = " ".join(args).strip() or os.path.basename(target)
    others = [e for e in os.listdir(target) if not e.startswith(".")]
    added, kept = scaffold(target, name)
    if sys.platform == "win32":
        cpath = project_config_path(target)
        pc = load_json(cpath, {}) or {}
        pc.setdefault("gate", {}).setdefault("env", {}).setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        save_json(cpath, pc)
    if ".gitignore" in kept and ensure_gitignore(target):
        print("  + .gitignore に標準の除外設定を追記")
    info(f"初期化しました: {target}")
    for a in added:
        print(f"  + {a}")
    for k in kept:
        print(f"  = {k}（既存のため変更なし）")
    if others:
        print(f"  ! 既存のファイルが {len(others)} 件あります。新規開発の前提と合うか確認してください。")
    print(target)


def cmd_create(args):
    ensure_home()
    parent = pop_opt(args, "--parent")
    name = " ".join(args).strip()
    if not name:
        die('アプリ名を指定してください: create "<app名>"')
    cfg = config()
    parent = os.path.abspath(os.path.expanduser(parent or cfg.get("workspace_dir") or os.getcwd()))
    target = os.path.join(parent, slugify(name) or "app")
    if os.path.exists(target) and os.listdir(target):
        die(f"既に存在し、空ではありません: {target}（別の名前にするか --parent を指定）")
    if find_project(parent):
        die(f"既存のE-AQDLプロジェクトの内側には作れません: {find_project(parent)}")
    os.makedirs(target, exist_ok=True)
    scaffold(target, name)
    print(target)
