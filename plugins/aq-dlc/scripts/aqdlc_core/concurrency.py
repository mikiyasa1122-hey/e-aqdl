"""並行処理・共有リソースのリスク（改善B）: リスクの種類で分類し、その種類があるときだけ詳しい確認を求める。

- 要求担当は、更新の対象・共有状態・複数の書き手・DB の外の副作用があるリスクにだけ、種類（--concurrency）と、
  状態遷移の順序（--scenario）・壊れてはいけない不変条件（--invariant）を付ける。並行処理と関係のない変更では
  何も増えない（種類のないリスクは従来どおり）。
- 種類のあるリスクの検証には、処理の順序をどう決めるか（--ordering。差し込み口・バリア・ロック待ちなど。
  固定時間の sleep に頼らない）を求める。
- レビュー担当は、報告の「### 競合シナリオ」に、そのリスクごとに順序・不変条件・結果（問題なし／要検証／問題あり）・
  根拠を書く。「問題あり」は修正必須の指摘、「要検証」（再現の根拠が弱い）は修正必須か証拠不足の指摘と対応させる。
  そのリスクを対象にした修正必須の指摘には、順序・不変条件・再現テスト案の欄を求める。
- エンジンが確かめるのは、欄があることと対応が取れていることだけ。テストが本当に順序を制御しているか、
  レビューの判断が正しいかは保証しない。

このモジュールは状態を書き換えない純粋関数だけを持つ。
"""
import re

KINDS = {
    "toctou": "TOCTOU（確認の結果が、更新の時点まで有効か）",
    "lost_update": "更新の競合（lost update。後から書いた側が先の更新を消さないか）",
    "duplicate": "二重実行・再試行による重複登録（冪等性・重複の防止）",
    "transaction": "トランザクションの境界（途中の失敗で不整合が残らないか）",
    "locking": "ロック・ロックの順序・デッドロック・分離レベル・タイムアウト",
    "shared_resource": "共有ファイル・参照カウント・複数の API やジョブから更新される資源の削除の競合",
    "tenant_boundary": "マルチテナントの境界・認可の確認の後の状態の変化・ID の取り違え",
    "side_effect": "DB の外の副作用（ストレージ・外部 API）と DB の更新の、失敗時の整合",
}
RESULTS = ("問題なし", "要検証", "問題あり")
SECTION = "競合シナリオ"
SOLUTIONS = ("楽観的排他（version）", "条件付き更新", "ユニーク制約", "冪等キー", "トランザクション", "キュー", "ロック")


def parse_kinds(text):
    """「toctou,lost_update」→ (種類の列, 不明な値の列)。"""
    vals = [v.strip() for v in re.split(r"[,、\s]+", text or "") if v.strip()]
    return [v for v in vals if v in KINDS], [v for v in vals if v not in KINDS]


def risks(state):
    """種類の付いたリスク: [(要求, リスク)]。"""
    return [(r, k) for r in state.get("requirements", []) for k in r.get("risks", []) if k.get("concurrency")]


def graph_issues(state):
    out = []
    for _r, k in risks(state):
        if not (k.get("scenario") or "").strip():
            out.append(f"{k['id']}（並行・共有）に状態遷移の順序（--scenario）がない")
        if not (k.get("invariant") or "").strip():
            out.append(f"{k['id']}（並行・共有）に壊れてはいけない不変条件（--invariant）がない")
        vs = k.get("verifications", [])
        if vs and not any((v.get("ordering") or "").strip() and v.get("level") != "manual" for v in vs):
            out.append(f"{k['id']}（並行・共有）の検証に、処理の順序の決め方（verify ... --ordering）がない")
    return out


def _field(text, name):
    m = re.search(rf"(?<![一-龥ぁ-んァ-ヶー]){re.escape(name)}\s*[:：]\s*([^/／]*)", text or "")
    return (m.group(1).strip(" *`…・") if m else "") or None


def scenario_entries(review_text, exact_section):
    """「### 競合シナリオ」の行: {リスクID: {"順序", "不変条件", "結果", "根拠"}}。"""
    sec = exact_section(review_text or "", SECTION)
    out = {}
    for ln in (sec or "").splitlines():
        m = re.match(r"^\s*[-*・]\s*\**\s*([A-Za-z][A-Za-z0-9_.-]*)\s*\**\s*[:：]\s*(.*)", ln)
        if not m:
            continue
        body = m.group(2)
        out[m.group(1)] = {k: _field(body, k) for k in ("順序", "不変条件", "結果", "根拠")}
    return out, sec is not None


def _targets(f):
    tg = re.search(r"対象\s*[:：]\s*([^/／]*)", f.get("text") or "")
    return set(re.findall(r"[A-Za-z][A-Za-z0-9_.-]*", tg.group(1))) if tg else set()


def review_issues(state, review_text, findings, exact_section):
    """レビューの報告の、並行・共有のリスクについての不足（なければ空）。種類の付いたリスクがなければ何も求めない。"""
    rs = risks(state)
    if not rs:
        return []
    errs = []
    entries, has = scenario_entries(review_text, exact_section)
    if not has:
        return [f"「### {SECTION}」の節がない（並行・共有のリスク {', '.join(k['id'] for _r, k in rs)} ごとに、"
                "順序・不変条件・結果・根拠を書く）"]
    for _r, k in rs:
        e = entries.get(k["id"])
        if not e:
            errs.append(f"「### {SECTION}」に {k['id']} の行がない")
            continue
        lack = [n for n in ("順序", "不変条件", "結果", "根拠") if not e.get(n)]
        if lack:
            errs.append(f"「### {SECTION}」の {k['id']} に欄がない・空: " + "、".join(lack))
            continue
        res = e["結果"]
        if res not in RESULTS:
            errs.append(f"「### {SECTION}」の {k['id']} の結果「{res}」は不正（{' / '.join(RESULTS)} のどれか）")
            continue
        about = [f for f in findings if k["id"] in _targets(f)]
        if res == "問題あり" and not any(f.get("action_type") == "fix_required" for f in about):
            errs.append(f"{k['id']} の結果が「問題あり」なのに、{k['id']} を対象にした修正必須の指摘がない")
        if res == "要検証" and not any(f.get("action_type") in ("fix_required", "missing_evidence") for f in about):
            errs.append(f"{k['id']} の結果が「要検証」なのに、{k['id']} を対象にした指摘（修正必須か証拠不足）がない")
    ids = {k["id"] for _r, k in rs}
    for f in findings:
        if f.get("action_type") != "fix_required" or not (_targets(f) & ids):
            continue
        lack = [n for n in ("順序", "不変条件", "再現テスト") if not _field(f.get("text"), n)]
        if lack:
            errs.append(f"[{f['label']}]（並行・共有の修正必須）に欄がない・空: " + "、".join(lack)
                        + "（発生条件・状態遷移の順序・壊れる不変条件・影響・根拠・再現できるテスト案を書く）")
    return errs


# ------------------------------------------------------------------ 引き継ぎ
REQUIREMENTS_LINES = [
    "3b. 並行処理・共有リソースの分類: 更新の対象・共有状態・複数の書き手（API・ジョブ・別の利用者）・DB の外の副作用が"
    "あるリスクにだけ、`$AQ risk R1 K2 \"<失敗>\" --concurrency <種類> --scenario \"<状態遷移の順序>\" "
    "--invariant \"<壊れてはいけない不変条件>\"` で種類を付ける。種類: " + "、".join(f"{k}（{v}）" for k, v in KINDS.items())
    + "。その検証には `--ordering \"<処理の順序の決め方（差し込み口・バリア・ロック待ちなど）>\"` を付ける。"
    "**並行処理と関係のない変更では付けない**（付けると、レビューと検証の手間が増える）。",
]


def brief_lines(state, role):
    rs = risks(state)
    if not rs:
        return []
    out = ["", "## 並行処理・共有リソースのリスク（種類の付いたリスクだけ。詳しく確かめる）"]
    for r, k in rs:
        kinds = "、".join(KINDS[x] for x in k.get("concurrency", []))
        out.append(f"- {k['id']}（{r['id']}）{k.get('text')} — 種類: {kinds}")
        out.append(f"  - 順序: {k.get('scenario') or '（未記入）'} / 不変条件: {k.get('invariant') or '（未記入）'}")
        for v in k.get("verifications", []):
            out.append(f"  - 検証 {v['id']}: {v.get('method')}（順序の決め方: {v.get('ordering') or '未記入'}）")
    if role == "design":
        out += ["- 直し方は、問題に合った最小のものを比べて選ぶ（" + "・".join(SOLUTIONS) + "）。ロックを一律に足さない。"
                "選択でコストや運用が意味のある形で変わるなら `$AQ dec` で人に選んでもらう。"]
    elif role == "implementer":
        out += ["- 検証のテストは、処理の順序を差し込み口・バリア・イベント・ロック待ちで決める。固定時間の sleep に頼らない。"
                "待機には必ず期限を付け、失敗しても待機とロックを解放する（try/finally）。",
                "- 期待する失敗（例外）・ロールバック・最終状態（DB に何が残るか）を、すべて確かめる。",
                "- 例: プラグインの `templates/examples/concurrency/`（TOCTOU・lost update・二重登録・途中の失敗・同時の書き込み）。"]
    elif role == "reviewer":
        out += ["- 上の種類ごとに確かめる: " + "；".join(KINDS[x] for x in sorted({x for _r, k in rs for x in k["concurrency"]})),
                "- 報告に「### 競合シナリオ」の節を足し、上のリスクごとに1行書く（エンジンが確かめる）:",
                "  `- K2: 順序: <処理A の確認 → 処理B の更新 → 処理A の書き込み のような順序> / 不変条件: <…> / "
                "結果: 問題なし|要検証|問題あり / 根拠: <コードの経路・テスト・実行結果>`",
                "- 「問題あり」は、そのリスクを対象にした修正必須の指摘にする。再現の根拠が弱いときは確定のバグにせず「要検証」とし、"
                "修正必須か証拠不足の指摘（必要な検証）にする。",
                "- そのリスクを対象にした修正必須の指摘には、成立条件・影響・根拠に加えて「順序: … / 不変条件: … / "
                "再現テスト: …」の欄を書く。",
                "- 直し方は、ロックを一律に勧めず、問題に合った最小のものを提案する（" + "・".join(SOLUTIONS) + "）。"]
    return out
