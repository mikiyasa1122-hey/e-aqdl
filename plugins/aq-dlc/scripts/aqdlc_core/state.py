"""run の状態の入口（Evidence Graph・証拠の鮮度・次の工程）。

実装は次のモジュールに分けた（ARCH-02）。ここは既存の名前をそのまま使えるようにする薄い入口。
- state_model: 状態の辞書の定数・読み取り（ファイルを読み書きしない）
- state_store: 保存場所・読み込み・保存・ロック・整合性の検証の呼び出し・監査ログ・移行・新しい run・Ctx（指紋の計算）
- state_policy: 判定（整合性の規則・gate の鮮度・完了までに足りないもの）。ファイル・git に触れない
- state_view: 人間・担当への文章

Evidence Graph:
  要求(R) ─┬─ 受け入れ条件(AC) ── 検証(V) ── テスト(command/checks/test) ── gate 結果
           └─ リスク(K)        ── 検証(V) ── ...
  要求(R) ── 設計要素（design-map） ── 実装ファイル（impl-map）

各工程の成果物（仕様書・設計書・実装報告・レビュー結果）は、作成時点の
仕様・設計・コード・検証条件の指紋と一緒に記録する。指紋が今の状態と一致する
ものだけを証拠として扱う。
"""
# 既存の import 先（`from .state import ...`）を変えずに使えるよう、移した名前をここから出す。
# 依存の向き: state → state_view → state_policy → state_model、state → state_store → state_policy・state_model
from .decision_sheet import other_options, recommended_option  # noqa: F401  判断シートの規則（cli からも使う）
from .util import die
from .state_model import (AGENT_CALL, EAQDL, ID_RE, PHASE_OF_ROUTE, ROLES, ROLE_LABEL, SCHEMA, STATUSES, bump, deferred_list, deferred_ok, graph_fp, human_event, id_index, iter_nodes, iter_verifications, last_human_seq)  # noqa: F401
from .state_policy import (P, _problems, current_review, gate_status, graph_issues, last_impl, latest_review, open_violations, problems, review_passed, running_agents, validate_state)  # noqa: F401
from .state_store import (Ctx, StateError, _audit_state, actor_hint, artifacts_dir, doc_hash, doc_path, empty_state, load_state, log, migrate, new_run, require_state, run_dir, runs_dir, save_state, spec_hash)  # noqa: F401
from .state_view import (describe_state, first_line, instruction, next_action, pending_by_sheet, spec_sheet, trace_rows)  # noqa: F401


def find(state, nid, kinds=None):
    idx, dups = id_index(state)
    if nid in dups:
        die(f"ID {nid} が重複しているため特定できません。重複を解消してください。")
    if nid not in idx or (kinds and idx[nid][0] not in kinds):
        die(f"{'・'.join(kinds) if kinds else 'ノード'} が見つかりません: {nid}")
    return idx[nid]


def ensure_new_id(state, nid, same_place=None):
    """ID は run 全体で一意。same_place（kind, parent）と一致する既存ノードの更新だけ許す。"""
    if not ID_RE.match(nid or ""):
        die(f"ID の形式が不正です: {nid}（英字で始まる英数字・._- の32文字以内）")
    idx, _ = id_index(state)
    if nid in idx:
        kind, _node, parent = idx[nid]
        if same_place and (kind, parent) == same_place:
            return idx[nid][1]
        die(f"ID {nid} は既に使われています（{kind}{'、親 ' + parent if parent else ''}）。"
            "検証結果が上書きされるのを防ぐため、別のIDを付けてください。")
    return None


def req_by_id(state, rid):
    for r in state["requirements"]:
        if r["id"] == rid:
            return r
    die(f"要求が見つかりません: {rid}")


