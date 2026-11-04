"""リスクに応じた工程の選択（Risk-Adaptive Workflow）。

工程（モード）:
  lite      要求整理（仕様の承認）→ 実装 → gate（設計担当・独立レビューを使わない）
  standard  lite ＋ 独立レビュー（設計担当を使わない）
  strict    すべて（要求整理 → 設計 → 実装 → gate → 独立レビュー）。従来の流れと同じ
設定 workflow.mode:
  full（既定）       常に strict と同じ工程（従来どおり。旧 run の互換）
  auto               要求・受け入れ条件・リスクの文と、実装の対象ファイルから決定論的に選ぶ
  lite / standard / strict   固定

安全側の原則:
  - 判定に必要な情報がない・読めないときは strict。
  - 認証・認可・秘密情報・決済・削除・DB のスキーマ・並行処理などの語、または該当するファイルがあれば strict。
  - 実装の対象ファイルで上位の工程が必要になったら、自動で上げる（下げるのは人間の承認だけ）。
  - 省いた工程は「省略（工程の選択による）」と記録し、合格・確認済みとは書かない。
  - 隠し受け入れテスト（holdout）が有効なら、テストを書く設計担当は省かない。
"""
import fnmatch
import re

from .util import fingerprint

ORDER = ("lite", "standard", "strict")
MODES = ORDER
STAGES = {
    "lite": {"design": False, "review": False},
    "standard": {"design": False, "review": True},
    "strict": {"design": True, "review": True},
}

STRICT_WORDS = [
    "認証", "認可", "権限", "ログイン", "パスワード", "トークン", "セッション", "秘密", "機密", "個人情報", "暗号",
    "決済", "課金", "支払", "請求", "送金", "残高", "在庫の引当",
    "削除", "退会", "マイグレーション", "スキーマ", "テーブル", "トランザクション", "整合性",
    "並行", "同時", "排他", "競合", "ロック", "マルチテナント", "テナント", "外部サービスへの書き込み", "本番",
    "auth", "login", "password", "token", "session", "secret", "credential", "permission", "oauth", "jwt",
    "payment", "billing", "migration", "schema", "transaction", "concurren", "race", "lock", "tenant", "delete",
]
STRICT_PATHS = ["**/auth/**", "**/*auth*", "**/security/**", "**/permission*", "**/middleware/**",
                "**/migrations/**", "**/*.sql", "**/models/**", "**/db/**", "**/schema*", "**/prisma/**",
                "**/payment*/**", "**/*payment*", "**/billing/**", "**/workers/**", "**/queue*/**", "**/jobs/**",
                ".github/workflows/**", "**/Dockerfile", "Dockerfile", "**/*.tf", "**/k8s/**"]
LITE_MAX_REQUIREMENTS = 3


def config_mode(cfg):
    m = str((cfg.get("workflow") or {}).get("mode") or "full")
    return m if m in ("full", "auto") + MODES else "full"


def _texts(state):
    for r in state.get("requirements", []):
        yield r.get("text") or ""
        for a in r.get("acceptance", []) + r.get("risks", []):
            yield a.get("text") or ""


def _word_hits(state):
    hits = []
    for t in _texts(state):
        low = t.lower()
        for w in STRICT_WORDS:
            if w in t or w in low:
                hits.append(w)
    return sorted(set(hits))


def _match(path, pats):
    p = path.replace("\\", "/")
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch("/" + p, pat) for pat in pats)


def impl_files(state):
    return sorted({f for fs in ((state.get("impl") or {}).get("map") or {}).values() for f in fs})


def path_hits(state):
    return [f for f in impl_files(state) if _match(f, STRICT_PATHS)]


def classify(state):
    """要求の文から工程を選ぶ（決定論的）。戻り値: (mode, 理由の一覧)"""
    reqs = state.get("requirements") or []
    if not reqs:
        return "strict", ["要求が未登録（判定に必要な情報がない）"]
    words = _word_hits(state)
    if words:
        return "strict", [f"リスクの高い語: {', '.join(words[:6])}"]
    # 「重要」の印（critical）は使わない: 要求分析担当は主な要求にほぼ必ず付けるため、使うと lite が選ばれなくなる
    # （実験11）。危険の判定はリスクの高い語と実装の対象ファイルで行う
    if len(reqs) <= LITE_MAX_REQUIREMENTS:
        return "lite", [f"要求 {len(reqs)} 件・リスクの高い語なし"]
    why = [f"要求 {len(reqs)} 件（lite は {LITE_MAX_REQUIREMENTS} 件まで）"]
    return "standard", why + ["リスクの高い語なし"]


def higher(a, b):
    return a if ORDER.index(a) >= ORDER.index(b) else b


def effective(state, cfg):
    """この run の工程。戻り値: {"mode", "source", "reasons", "stages"}"""
    cm = config_mode(cfg)
    if cm == "full":
        return {"mode": "strict", "source": "config", "reasons": ["設定 workflow.mode=full（従来どおり全工程）"],
                "stages": dict(STAGES["strict"]), "config": cm}
    plan = state.get("workflow_plan") or {}
    if plan.get("human_mode") in MODES:
        mode, source, reasons = plan["human_mode"], "human", [f"人間が選んだ（{plan.get('human_note') or ''}）".strip()]
    elif cm in MODES:
        mode, source, reasons = cm, "config", [f"設定 workflow.mode={cm}"]
    else:
        try:
            mode, reasons = classify(state)
        except Exception as e:  # noqa: BLE001 — 判定できなければ安全側
            mode, reasons = "strict", [f"判定に失敗した（{e}）"]
        source = "auto"
    if source != "human":
        ph = path_hits(state)
        if ph and mode != "strict":
            mode, reasons = "strict", reasons + [f"実装の対象ファイルで上げた: {', '.join(ph[:4])}"]
            source = source + "+upgrade"
    stages = dict(STAGES[mode])
    from . import holdout as HO
    if HO.enabled(cfg) and not stages["design"]:
        stages["design"] = True
        reasons = reasons + ["隠し受け入れテストを書く設計担当は省かない"]
    return {"mode": mode, "source": source, "reasons": reasons, "stages": stages, "config": cm}


def needs(state, cfg, stage):
    return effective(state, cfg)["stages"].get(stage, True)


def mode_target(state, mode):
    """工程を下げる承認の対象 ID（MODE-<工程>-<run の指紋8桁>）。"""
    return f"MODE-{mode}-{fingerprint(str(state.get('run_id')))[:8]}"


def describe(state, cfg):
    e = effective(state, cfg)
    skipped = [k for k, v in (("設計", e["stages"]["design"]), ("独立レビュー", e["stages"]["review"])) if not v]
    return (f"工程: {e['mode']}（{'・'.join(e['reasons'])}）"
            + (f"。省略: {'・'.join(skipped)}（工程の選択による。確認済みではない）" if skipped else ""))
