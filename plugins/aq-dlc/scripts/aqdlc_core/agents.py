"""サブエージェントの役割・引き継ぎ・成果物の検証・権限。

  起動前（PreToolUse: Agent）  … 前の工程が揃っているか確認。並列起動を拒否し、前景で実行させる
  起動時（SubagentStart）      … 引き継ぎ情報（仕様・設計の場所と指紋、指摘、未設定の検証など）を注入
  終了時（SubagentStop）       … 成果物を検証。不足があれば終了させずに続けさせる。揃えば登録する
  実行中（PreToolUse: Bash / Write / Edit） … 役割ごとに書き込み先とエンジンのコマンドを制限
"""
import datetime as _dt
import os
import re
import shutil
import time

from . import gate as G
from . import verdict as V
from . import counterfactual as CF
from . import roles as RL
from .project import config, engine_cmd
from .state import (ROLES, ROLE_LABEL, Ctx, artifacts_dir, bump, doc_hash, doc_path, gate_status, open_violations,
                    current_review, graph_issues, id_index, iter_verifications, last_impl, latest_review, problems,
                    review_passed,
                    run_dir, running_agents, save_state)
from .util import load_json
from .util import now_iso, read_text, short, write_text, is_within, norm

PREFIX = "aq-dlc:"


def role_of(agent_type):
    if not agent_type:
        return "orchestrator"
    # プラグインのエージェント（aq-dlc:<担当>）だけを担当として扱う。接頭辞のない同名のエージェント
    # （.claude/agents/implementer.md など）は担当外（other）とみなし、成りすましを防ぐ
    if agent_type.startswith(PREFIX) and agent_type[len(PREFIX):] in ROLES:
        return agent_type[len(PREFIX):]
    return "other"


# ------------------------------------------------------------------ markdown helpers
def headings(md):
    return [ln.lstrip("#").strip() for ln in (md or "").splitlines() if re.match(r"^#{1,4}\s", ln)]


def missing_headings(md, required):
    hs = headings(md)
    return [r for r in required if not any(r in h for h in hs)]


def section(md, keyword):
    lines = (md or "").splitlines()
    out, on, level = [], False, 0
    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            if on and len(m.group(1)) <= level:
                break
            if keyword in m.group(2) and not on:
                on, level = True, len(m.group(1))
                continue
        if on:
            out.append(ln)
    return "\n".join(out).strip() if on else None


def exact_section(md, title):
    """見出しの文字が title と完全に一致する節の本文（なければ None）。"""
    out, on, level = [], False, 0
    for ln in (md or "").splitlines():
        m = re.match(r"^(#{1,4})\s+(.*?)\s*#*\s*$", ln)
        if m:
            if on and len(m.group(1)) <= level:
                break
            if not on and m.group(2).strip().strip("*") == title:
                on, level = True, len(m.group(1))
                continue
        if on:
            out.append(ln)
    return "\n".join(out) if on else None


def hash_line_ok(md, label, full):
    if not full:
        return False
    m = re.search(rf"{label}\s*[:：]\s*`?([0-9a-f]{{8,40}})", md or "")
    return bool(m) and full.startswith(m.group(1))


def bullet_items(text):
    items = []
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*[-*・]\s*(.+)", ln)
        if m:
            t = m.group(1).strip()
            if t and not re.match(r"^(なし|特になし|無し|none)\b", t, re.I):
                items.append(t)
    return items


REQUIRED = {
    "requirements": ["目的", "機能要件", "非機能要件", "受け入れ条件", "リスク", "不明点", "対象外"],
    "design": ["技術スタック", "アーキテクチャ", "ディレクトリ構成", "データ", "エラー", "セキュリティ",
               "テスト方針", "要求との対応", "設計判断"],
    "implementer": ["実施内容", "検証結果", "差し戻し"],
}


# ------------------------------------------------------------------ what each role must fix now
def pending_fix(ctx):
    """実装担当に今回求める修正（レビュー指摘・gate 不合格・CI 不合格）。"""
    s = ctx.state
    rv = current_review(s)
    li = last_impl(s)
    items, kind = [], None
    unowned = []
    if li and li.get("code_fp") and li["code_fp"] != ctx.code_fp:
        c, a, r = G.snapshot_diff(li.get("snapshot") or {}, ctx.snap)
        unowned = c + a + r or ["（git diff で確認）"]
    for v in open_violations(s):
        unowned += [f for f in v.get("files", []) if f not in unowned]
    if unowned:
        kind = ("unowned", li["n"] if li else 0)
        items = [{"label": f"担当外{i + 1}", "text": f"{f}（実装担当以外の操作で変更・追加・削除された。"
                  "内容を確認し、正しければ取り込み、不要なら元に戻す）"} for i, f in enumerate(unowned[:20])]
    if rv and not review_passed(rv) and (not li or li["seq"] < rv["seq"]):
        kind = kind or ("review", rv["n"])
        items += [f for f in rv.get("findings", []) if f.get("severity") == "重大"] or rv.get("findings", [])
    if kind:
        if kind[0] == "unowned":  # 担当外の変更の後の gate の不合格も合わせて伝える
            items += [dict(f, info=True) for f in _gate_items(s, li)[1]]  # 参考（対応の記載は必須にしない）
        return kind, items
    crec = CF.current(s)
    if crec and CF.cf_config(ctx.cfg)["mode"] == "enforce" and crec.get("status") == "done" \
            and (not li or li["seq"] < crec.get("seq", 0)):
        surv = [m for v, _w, ids in CF.critical_gaps(s, crec) for m in crec["mutations"] if m["id"] in ids]
        if surv:
            return ("counterfactual", crec["stamp"]), [
                {"label": f"反証{m['id']}", "text": f"{m['id']}（{m['verification']} / {m.get('risk') or m['parent']}）: "
                 f"{m['file']}:{m['line']} に「{m['category']}」の欠陥（`{m['original'].strip()}` → "
                 f"`{m['mutated'].strip()}`）を入れてもテストが通過した。この欠陥で失敗するようにテストを強化する"
                 "（実装を変えて見逃しを隠さない）"} for m in surv]
    return _gate_items(s, li)


def _gate_items(s, li):
    kind, items = None, []
    g = s.get("gate")
    if g and (not li or li["seq"] < g["seq"]):
        fl = V.failures(g.get("results"))
        failed, vfail = fl["checks"], fl["verifications"]
        md = g.get("modified_during_gate")
        if md:
            failed = [("gate中の変更", {"error": f"gate の実行中に変わった: {md.get('summary')}（テスト・ビルドが追跡対象の"
                                              "ファイルを書き換えないようにする）"})] + failed
        hfail, qfail = fl["holdout"], fl["quality"]
        if failed or vfail or hfail or qfail:
            kind = ("gate", g["stamp"])
            items = [{"label": n, "text": V.reason_of(r)} for n, r in failed + vfail]
            # 隠しテストは、どの受け入れ条件かだけを返す（テストの中身・出力は見せない）
            items += [{"label": f"隠しテスト {ac}", "text": f"受け入れ条件 {ac} の隠し受け入れテストが不合格。中身は見せない。"
                       f"仕様の {ac} と設計書の契約（名前・入出力・エラー）どおりに動くかを見直す"} for ac in hfail]
            for key, label, r in qfail:
                items.append({"label": label, "text": f"{V.reason_of(r)}。" + _quality_fix_hint(key, r)})
    return kind, items


# ------------------------------------------------------------------ briefs
def role_selection(role, ctx, kind=None, prompt=""):
    """この担当に今回適用する Role Profile（決定論的。規則は roles.select_for にある）。"""
    return RL.select_for(role, ctx.state, ctx.cfg, kind, prompt)


def brief(role, ctx, entry=None, sel=None):
    s, project, rd = ctx.state, ctx.project, ctx.rd
    aq = f'{engine_cmd()} <コマンド> --project "{project}"'
    head = [
        f"【E-AQDL 引き継ぎ情報】あなたは E-AQDL の {ROLE_LABEL[role]} です。",
        f"- プロジェクト: {project}",
        f"- run: {s['run_id']}（{s.get('title')}）",
        f"- エンジン: {aq}  （以下 $AQ と書く。必ず --project を付ける）",
        f"- 成果物の置き場所: {artifacts_dir(rd)}",
        "- 言語: 報告・成果物・コード内のコメントはすべて日本語で書く。",
        "- 人間には質問できない。人間の判断が必要なことは成果物に書いて統括に返す。",
        "- state.json と config.json は直接編集しない（エンジンのコマンドで更新する）。",
        "- 終了時にエンジンが成果物を検証する。不足があると終了できず、理由が返ってくるので直して終える。",
        "",
    ]
    compact = bool((ctx.cfg.get("cost_optimization") or {}).get("compact_handoff"))
    if compact:
        # エージェント定義（agents/*.md、常に読み込まれる）と重複する一般的な決まりは省く。run 固有の情報は残す
        head = head[:5] + ["- 言語・state.json の扱い・人間への質問・終了時の検証は、エージェント定義の「守ること」のとおり。", ""]
    body = globals()[f"_brief_{role}"](ctx, compact) if role == "implementer" else globals()[f"_brief_{role}"](ctx)
    if role == "reviewer" and entry:
        body += _reviewer_extra(ctx, entry)
    if sel:
        body += RL.brief_section(sel, compact=compact)
    return "\n".join(head + body)


def _req_lines(s, with_ac=True):
    out = []
    for r in s["requirements"]:
        out.append(f"- {r['id']}{'【重要】' if r.get('critical') else ''}"
                   f"{'（非機能）' if r.get('kind') == 'nonfunctional' else ''} {r.get('text')}")
        if with_ac:
            for a in r.get("acceptance", []):
                out.append(f"    - {a['id']}: {a.get('text')}")
    return out or ["（まだ登録されていない）"]


def _brief_requirements(ctx):
    s = ctx.state
    sp = doc_path(ctx.rd, "spec")
    lines = [
        "## あなたの仕事",
        "統括から渡されたユーザーの依頼を分析し、人間が承認できる仕様を作る。",
        "**曖昧な点を勝手に確定しない。** 答えによって作るものが変わる点は question にする。",
        "",
        "1. 要求: `$AQ req R1 \"<要求>\" [--critical] [--kind nonfunctional]`（性能・セキュリティ・対応環境などは非機能）",
        "2. 受け入れ条件: `$AQ accept R1 AC1 \"<観察できる条件>\"`（全要求に1つ以上）",
        "3. Failure First: `$AQ risk R1 K1 \"<起こりうる失敗>\"`（重要な要求には必須。不正入力・権限・境界値・"
        "データ不整合・エラー時・秘密情報・同時操作の観点で洗い出す）",
        "4. 検証方法: `$AQ verify AC1 V1 \"<確かめ方>\" --level unit|integration|e2e|manual [--after-merge]`"
        "（受け入れ条件・リスクごとに1つ以上。実行コマンドは実装担当が後で設定する。"
        "人にしか確かめられないものは `--manual \"<手順>\"`、デプロイ後にしか確かめられないものは `--after-merge`）。"
        "**機械で確かめられること（import の確認、ファイルの有無、テストの依存など）を `--manual` にしない**: "
        "人の確認は1件ごとに人間との往復が増える。人の確認は、本番環境・外部サービス・見た目・使い勝手など、機械で確かめられないものだけ",
        "5. 不明点・矛盾点: `$AQ question Q1 \"<質問>\" --options \"<推奨案>（推奨）|<案2>|<案3>\"`（最大4問程度。重要な順）。"
        "**推奨を必ず1つ付け（印は選択肢の末尾の「（推奨）」。2つ以上は登録できない）、仕様書は推奨を前提に書く**（人間は仕様の承認と同じ返答で質問にも答えるので、往復が1回で済む。"
        "推奨と違う答えが来たら、そのとき直す）。推奨を決められない質問だけ、推奨なしで登録する（仕様の提示の前に人間に聞く）。",
        "6. 利用者に見える小さな決めごと（ほぼ確実なもの）: `$AQ assume \"<内容>\"`、作らないもの: `$AQ assume \"<内容>\" --out`",
        "7. 不要になった項目: `$AQ drop <ID>`（理由は仕様書に書く）",
        f"8. 仕様書を `{sp}` に書く。必須の見出し: 目的 / 機能要件 / 非機能要件 / 受け入れ条件 / リスク / "
        "不明点・矛盾点 / 対象外。登録した要求・受け入れ条件のIDを本文に必ず含める。",
        "",
        "ID は run 全体で一意（重複は拒否される）。現在の内容は `$AQ spec` と `$AQ trace` で確認できる。",
        "",
        "## 現在の要求",
    ] + _req_lines(s)
    answered = [q for q in s["questions"] if q.get("status") == "answered"]
    if answered:
        lines += ["", "## 人間の回答（仕様に反映すること）"]
        lines += [f"- {q['id']} {q['text']} → 回答: {q.get('answer')}" for q in answered]
    rv = latest_review(s)
    if rv and rv.get("blocking", 0) > 0:
        spec_f = [f for f in rv.get("findings", []) if f.get("category") == "仕様"]
        if spec_f or rv.get("missing_risks"):
            lines += ["", f"## レビュー#{rv['n']} の指摘（仕様・リスクに関するもの）"]
            lines += [f"- [{f['label']}] {f['text']}" for f in spec_f]
            lines += [f"- 不足しているリスク: {m}" for m in rv.get("missing_risks", [])]
    lines += ["", "## 終了時の報告",
              "作った要求・受け入れ条件・リスク・検証・質問の数と、統括への申し送り（人間に確認すべきこと）を短く書く。"]
    return lines


def _coverage_lines(ctx):
    from . import quality as Q
    c = Q.config_of(ctx.cfg)
    if not c["enabled"]:
        return []
    return ["", "## 差分カバレッジ（この run で変えた行のうち、テストで実行された行の割合）",
            f"- gate は `{c['report'] or '（未設定）'}` のカバレッジのレポート（coverage.py の JSON か LCOV）を読み、"
            f"基準 {c['min']:g}% 未満なら不合格にする。変えたファイルがレポートにない場合も不合格。",
            "- 単体テストのチェックで、このレポートを作るようにする（例: `COVERAGE_FILE=coverage/.coverage python3 -m coverage run"
            " -m pytest -q && COVERAGE_FILE=coverage/.coverage python3 -m coverage json -o coverage/coverage.json`。"
            "c8 / nyc / jest なら LCOV）。計測の一時ファイルは coverage/ など gate の指紋の対象外に置く。",
            "- 基準・対象外の設定は人が決める（変えない）。"]


def _brief_design(ctx):
    s = ctx.state
    lines = [
        "## あなたの仕事",
        f"承認済みの仕様 `{doc_path(ctx.rd, 'spec')}`（指紋 {short(ctx.spec_hash)}）に基づいて設計する。最初に仕様書を読む。",
        "**仕様は変更しない。** 仕様の変更が必要なら、設計書の「## 差し戻し」に理由を書いて終える（統括が人間と調整する）。",
        "",
        f"1. 設計書を `{doc_path(ctx.rd, 'design')}` に書く。先頭付近に「仕様: {short(ctx.spec_hash)}」の行を書く。",
        "   必須の見出し: 技術スタック / アーキテクチャ / ディレクトリ構成 / データモデル / エラーハンドリング / "
        "セキュリティ / テスト方針（単体・結合・E2E） / 要求との対応 / 設計判断とトレードオフ / 差し戻し（なければ「なし」）",
        "2. 要求と設計要素の対応: `$AQ design-map R1 \"<モジュール・ファイル・画面など>\"`（全要求）",
        "3. 内部の設計判断: `$AQ decision \"<判断と理由>\"`",
        "4. Quality Gate のチェック: `$AQ add-check <名前> \"<コマンド>\" --kind typecheck|lint|build|unit|integration|e2e"
        " [--needs <前提のチェック>]`",
        "   - 技術スタックに応じて、型チェック・Lint・単体・結合・ビルド・E2E（必要な場合）を登録する。",
        "   - E2E はビルドの後に実行され、ビルドが不合格なら実行されない（古い成果物での合格を防ぐ）。",
        "   - Webアプリの E2E は Playwright を候補にする（必須ではない）。",
        "   - テストファイルが0件だと失敗するランナーがある。そのチェックが最初から通るかを実装担当に申し送る。",
        "   - 静的解析（Lint・型）は、コマンドに `{changed:.拡張子}` を書くと、この run で変えたファイルだけが対象になる"
        "（例: `ruff check {changed:.py}`、`npx eslint {changed:.js,.ts}`）。既存のコードの古い問題で止めないため。"
        "変えたファイルに該当がなければ実行せず「対象なし」になる。",
        "5. 検証のテストレベル: `$AQ bind <検証ID> --level unit|integration|e2e`",
        "",
        "方針: 技術スタックは固定しない。対象に対して妥当な構成を選ぶ。過剰な抽象化・不要な依存を避け、"
        "保守性・可読性・テスト容易性を優先する。",
        "",
        "## 要求",
    ] + _req_lines(s)
    lines += ["", "## 検証（レベル未設定のものは決める）"]
    for _r, p, v in iter_verifications(s):
        lines.append(f"- {v['id']}（{p['id']}）{v.get('method')} レベル: {v.get('level') or '未設定'}")
    if s.get("decisions"):
        lines += ["", "## 既存の設計判断"] + [f"- {d['id']} {d['text']}" for d in s["decisions"]]
    rv = latest_review(s)
    if rv and rv.get("blocking", 0) > 0:
        df = [f for f in rv.get("findings", []) if f.get("category") == "設計"]
        if df:
            lines += ["", f"## レビュー#{rv['n']} の設計に関する指摘"] + [f"- [{f['label']}] {f['text']}" for f in df]
    lines += _coverage_lines(ctx)
    from . import holdout as HO
    if HO.enabled(ctx.cfg):
        d = HO.run_holdout_dir(ctx.project, s.get("run_id"))
        lines += ["", "## 隠し受け入れテスト（holdout）を作る",
                  f"- 置き場所: `{d}`（プロジェクトの外。実装担当からは読めない）。ここにテストファイルを書く。",
                  "- 受け入れ条件ごとに、設計書で決めた契約（モジュール名・関数・エンドポイント・入出力・エラー）だけを使って"
                  "外から確かめるテストを書く。境界値・権限・異常系など、実装がテストに合わせて弱めがちな点を入れる。",
                  "- 登録: `$AQ holdout <受け入れ条件ID> --command \"<コマンド>\"`（コマンドの {holdout} は置き場所に置き換わる。"
                  "例: `python3 -m pytest {holdout}/test_ac1.py -q`。作業フォルダはプロジェクト）",
                  "- 重要な要求の受け入れ条件には必ず1つ以上。テストの中身を設計書・プロジェクト内に書かない。"]
    lines += ["", "## 終了時の報告", "選んだ構成と理由、登録したチェック、実装担当への申し送りを短く書く。"]
    return lines


def _brief_implementer(ctx, compact=False):
    s = ctx.state
    sp, dp, ip = doc_path(ctx.rd, "spec"), doc_path(ctx.rd, "design"), doc_path(ctx.rd, "impl")
    kind, items = pending_fix(ctx)
    from . import workflow as WF
    wf = WF.effective(s, ctx.cfg)
    if not wf["stages"]["design"]:
        lines = [
            "## あなたの仕事",
            f"承認済みの仕様 `{sp}`（{short(ctx.spec_hash)}）に基づいて実装とテストを作る。**最初に仕様を読む。仕様を独断で変えない。**",
            f"この run の工程は {wf['mode']}（{'・'.join(wf['reasons'])}）で、**設計担当はいない**。次もあなたが行う:",
            "- 既存のコードの構成に合わせて、技術スタックと構成を決める。実装報告の「## 実施内容」に構成と理由を短く書く。",
            "- Quality Gate のチェックを登録する: `$AQ add-check <名前> \"<コマンド>\" --kind typecheck|lint|build|unit|integration|e2e`"
            "（少なくとも単体テスト。既存のテスト・Lint があれば含める。Lint は `{changed:.拡張子}` で変えたファイルだけにできる）",
            "- 要求と実装ファイルの対応（`$AQ impl-map`）・各検証の実行方法（`$AQ bind`）を設定する。",
            f"- 実装報告: `{ip}`。先頭に「仕様: {short(ctx.spec_hash)}」の行"
            + ("。「## 指摘への対応」に下の各ラベルごとの対応を書く" if kind else ""),
            "- 認証・権限・秘密情報・決済・削除・DB のスキーマ・並行処理に触れる必要が出たら、作業を止めて「## 差し戻し」に書く"
            "（工程を上げて設計とレビューを加える判断が必要）。",
            "禁止: テストを通すために検証条件を弱める・重要な検証を消す・テストを skip する。",
        ] + _coverage_lines(ctx) + [
            "",
        ]
        return lines + _impl_tail(ctx, s, kind, items)
    if compact:
        # 手順・禁止事項の一般論はエージェント定義（implementer.md）にある。読む版の指紋と報告の置き場所は残す
        lines = [
            "## あなたの仕事",
            f"承認済みの仕様 `{sp}`（{short(ctx.spec_hash)}）と設計書 `{dp}`（{short(ctx.design_hash)}）に基づいて"
            "実装とテストを作る。**最初に両方を読む。仕様・設計を独断で変えない。** 手順と禁止事項はエージェント定義のとおり。",
            f"- 実装報告: `{ip}`。先頭に「設計書: {short(ctx.design_hash)}」「仕様: {short(ctx.spec_hash)}」の行"
            + ("。「## 指摘への対応」に下の各ラベルごとの対応を書く" if kind else ""),
            "",
        ]
        return lines + _impl_tail(ctx, s, kind, items)
    lines = [
        "## あなたの仕事",
        f"承認済みの仕様 `{sp}`（{short(ctx.spec_hash)}）と設計書 `{dp}`（{short(ctx.design_hash)}）に基づいて"
        "実装とテストを作る。**最初に両方を読む。仕様・設計を独断で変えない。**",
        "",
        "1. 実装と、単体テスト・必要な結合テスト・必要なら E2E テストを書く。型安全性・エラー処理・セキュリティに配慮する。",
        "2. 要求と実装ファイルの対応: `$AQ impl-map R1 --files \"src/a.ts,src/b.ts\"`（全要求）",
        "3. 各検証の実行方法: `$AQ bind V1 --command \"<そのリスクだけを確かめるコマンド（テスト名で絞る）>\" "
        "--test \"<テストの場所>\"`",
        "   例: `npx vitest run -t \"保存失敗\"`、`npx playwright test -g \"保存失敗\"`、`pytest -k isolation`",
        "4. 自分でテストを実行して確認する（`$AQ gate` も実行できる）。",
        f"5. 実装報告を `{ip}` に書く:",
        f"   - 「設計書: {short(ctx.design_hash)}」「仕様: {short(ctx.spec_hash)}」の行（読んだ版の確認に使う）",
        "   - ## 実施内容 / ## 検証結果（自分で実行したコマンドと結果）/ ## 差し戻し（仕様・設計の変更が必要なもの。なければ「なし」）"
        + ("\n   - ## 指摘への対応（下の各項目のラベルごとに、何をどう直したか）" if kind else ""),
        "",
        "禁止: テストを通すために検証条件を弱める・重要な検証を消す・テストを skip する。"
        "bind やチェックの変更は履歴に残り、独立レビューで確認される。",
        "",
    ]
    return lines + _impl_tail(ctx, s, kind, items)


def _impl_tail(ctx, s, kind, items):
    """実装担当の引き継ぎのうち run 固有の部分（修正依頼・未設定の検証・要求・チェック）。省略しない。"""
    lines = []
    if kind:
        what = {"review": f"レビュー#{kind[1]} の重大な指摘", "counterfactual": f"反証テスト {kind[1]} の見逃し",
                "unowned": "担当外の変更の確認"}.get(kind[0], f"gate {kind[1]} の不合格")
        if kind[0] == "unowned" and any(not str(f["label"]).startswith("担当外") for f in items):
            what += "と、その後の gate の不合格"
        lines += [f"## 今回の修正依頼（{what}）"]
        for f in items:
            lines.append(f"- [{f['label']}] {f['text']}")
        lines.append("")
    from . import holdout as HO
    if HO.enabled(ctx.cfg) and HO.items(s):
        lines += ["## 隠し受け入れテスト（holdout）",
                  "次の受け入れ条件は、あなたからは見えないテストで gate のときに確かめる。中身は読めない（読もうとすると止まる）。"
                  "仕様と設計書の契約（名前・入出力・エラー）どおりに作る:",
                  "- " + "、".join(it["ac"] for it in HO.items(s)), ""]
    unbound = [v for _r, _p, v in iter_verifications(s) if not G.is_automated(v) and not v.get("manual")]
    if unbound:
        lines += ["## 実行方法が未設定の検証"] + [f"- {v['id']}: {v.get('method')}（レベル {v.get('level') or '未設定'}）"
                                              for v in unbound] + [""]
    lines += ["## 要求"] + _req_lines(s)
    lines += ["", "## Quality Gate のチェック"] + ([f"- {c['name']}（{c.get('kind', 'other')}）: {c['command']}"
                                                  for c in ctx.checks] or ["（未登録）"])
    return lines


def _cf_lines(ctx):
    cc = CF.cf_config(ctx.cfg)
    rec = CF.current(ctx.state)
    if cc["mode"] == "off":
        return ["", "## 反証テスト（Counterfactual Gate）",
                "- 無効（counterfactual.mode: off）。通常の gate の合格は「欠陥を検出できることの証明」ではない点に注意する。"]
    if not rec:
        return ["", "## 反証テスト（Counterfactual Gate）", "- 未実行"]
    sm = CF.summarize(rec)
    c = sm["counts"]
    lines = ["", f"## 反証テスト（Counterfactual Gate、mode: {cc['mode']}、{rec.get('stamp')}）",
             f"- killed {c['killed']} / survived {c['survived']} / invalid {c['invalid']} / timeout {c['timeout']}"
             f" / 対象外 {c['not_applicable']}（検出率 {sm['score'] if sm['score'] is not None else '算出不可'}"
             f"＝killed ÷（killed＋survived）、除外 {sm['excluded']} 件）"]
    for m in rec.get("mutations", []):
        if m.get("result") == "survived" and not m.get("equivalent"):
            lines.append(f"- survived {m['id']}: {m['file']}:{m['line']} {m['category']}（{m['verification']}）"
                         f" `{m['original'].strip()}` → `{m['mutated'].strip()}`")
    if c["survived"]:
        lines.append("→ survived の各 ID について、テストの不足か等価な変異かを「### 指摘」か「### 確認したこと」に書く"
                     "（ID を必ず書く）。重要な要求のテスト不足は重大な指摘にする。")
    return lines


def _quality_fix_hint(key, r):
    """品質の検査ごとの、修正の手がかり（なくてもよい）。"""
    if key == "diff_coverage":
        from . import quality as Q
        unc = "; ".join(f"{f}: {Q.ranges(ls)}" for f, ls in list((r.get("uncovered") or {}).items())[:6])
        return ((f"テストで実行されていない変更行: {unc}。" if unc else "")
                + "変えた行を確かめるテストを足す（基準・対象外の設定を変えて通さない）")
    return "基準・設定を変えて通さない"


def quality_summary_lines(g):
    """機械で確かめた差分の品質を、担当への文章にする（gate の結果の記録だけを読む。合否は計算しない）。"""
    from . import quality as Q
    res = (g or {}).get("results") or {}
    lines = []
    for n, r in (res.get("checks") or {}).items():
        if (r.get("scope") or {}).get("mode") != "changed" and r.get("scoped") is None:
            continue
        st = V.status_of(r)
        if st == V.NOT_APPLICABLE:
            lines.append(f"- チェック {n}（変えたファイルだけ）: 対象なし（該当する変更がない。実行していない）")
        else:
            lines.append(f"- チェック {n}（変えたファイル {r.get('scoped')} 件だけ）: {V.LABEL[st]}")
    dc = res.get("diff_coverage")
    if dc:
        st = V.status_of(dc)
        if dc.get("total"):
            lines.append(f"- 差分カバレッジ: {dc['percent']}%（{dc['covered']}/{dc['total']} 行）、基準 {dc['min']:g}%"
                         f" → {V.LABEL[st]}")
        else:
            lines.append(f"- 差分カバレッジ: {V.LABEL[st]}")
        if not V.passed(dc) and dc.get("reason"):
            lines.append(f"  - {dc['reason']}")
        for f, ls in list((dc.get("uncovered") or {}).items())[:10]:
            lines.append(f"  - テストで実行されていない変更行: {f}: {Q.ranges(ls)}")
        if dc.get("approx"):
            lines.append(f"  - 起点の内容を取り出せず、全行を変更として数えたファイル: {', '.join(dc['approx'][:5])}")
    return lines


def _reviewer_extra(ctx, entry):
    lines = []
    ql = quality_summary_lines(ctx.state.get("gate"))
    if ql:
        lines += ["", "## 機械で確かめた差分の品質（gate の結果。この run で変えた部分だけ）"] + ql + [
            "→ 静的解析が出す書式・Lint の個々の警告は指摘しない（機械に任せる）。あなたは仕様との整合・ロジック・"
            "テストが本当に確かめているかに集中する。テストで実行されていない変更行は、そこに重要な分岐がないかを見る。"
            "ただし、チェック・検証の対象や条件を、仕様の承認なしに狭めた・外した変更は、これまでどおり指摘する"
            "（機械の確認の範囲そのものは機械では確かめられない）。"]
    lines += _cf_lines(ctx)
    lines += ["", "## エンジンが機械的に検出したテストの兆候（判断材料。合否はあなたが判断する）"]
    sig = entry.get("test_signals") or []
    if sig:
        lines += [f"- {x['file']}: {x['issue']}" for x in sig]
        lines.append("→ 各ファイルについて、問題かどうかと理由を報告に書く（ファイル名を必ず書く）。")
    else:
        lines.append("- なし（skip・.only・アサーションなしのテストファイルは見つからなかった）")
    has_e2e = any(c.get("kind") == "e2e" for c in ctx.checks) or \
        any(v.get("level") == "e2e" for _r, _p, v in iter_verifications(ctx.state))
    lines += ["", "## E2E の要否"]
    if entry.get("web_app") and not has_e2e:
        lines.append("- Web アプリに見えるが、E2E のチェック・検証がない。重要な利用者の操作の流れについて E2E が必要かを評価し、"
                     "不要ならその理由を「### E2E の要否」に書く。必要なら重大な指摘にする。")
    else:
        lines.append(f"- E2E は{'あり' if has_e2e else 'なし'}。リスクに見合っているかを「### E2E の要否」に書く。")
    return lines


def review_blind(cfg):
    return bool((cfg.get("review") or {}).get("blind", True))


def _brief_reviewer(ctx):
    s = ctx.state
    g = s.get("gate") or {}
    prev = [r for r in s.get("reviews", []) if r.get("valid")]
    since = prev[-1]["seq"] if prev else 0
    lines = [
        "## あなたの仕事",
        "独立した品質レビュー。実装担当の自己評価や説明に頼らず、仕様・設計・ソースコード・実際の検証結果で判断する。",
        "**コードを変更しない。** Write / Edit は使えない。Bash でもファイルを書き換えない"
        "（レビュー中にコードが変わると、このレビューは無効になる）。",
        "",
        "## 読むもの",
        f"- 仕様: `{doc_path(ctx.rd, 'spec')}`（{short(ctx.spec_hash)}）",
        f"- 設計: `{doc_path(ctx.rd, 'design')}`（{short(ctx.design_hash)}）",
        (f"- 実装報告: `{doc_path(ctx.rd, 'impl')}`（参考。鵜呑みにしない）" if not review_blind(ctx.cfg) else
         "- 実装報告は渡さない（ブラインドレビュー）。impl.md と実装担当のメッセージは読まない（エンジンが読み取りを止める）。"
         "実装担当の説明ではなく、仕様・設計・コード・テスト・gate の結果で判断する"),
        "- 要求→受け入れ条件→設計→実装→テスト→結果の対応: `$AQ trace`",
        f"- 検証条件・チェックの変更履歴: `$AQ history --since {since}`（弱められた検証がないか）",
        f"- gate の結果: {g.get('stamp')}（ログ: {os.path.join(ctx.rd, 'gate')}）",
        "",
        "## 見ること（重要度順）",
        "1. 仕様との整合（受け入れ条件を満たしているか）  2. 設計との整合",
        "3. バグ・セキュリティ・認可・データ整合性・異常系",
        "4. テストの品質: 検証がリスクを本当に確かめているか（モックだけ、弱いアサーション、常に通る、skip）。"
        "E2E の不足。ビルド前の古い成果物に対するテスト",
        "5. テストや検証条件の改ざん・弱体化（git diff と history で確認）",
        "",
        "テストを実行するときは、出力を一時フォルダに出す（例: Playwright は `--output` に OS の一時フォルダ）。"
        "`$AQ gate` は実行しない。依存関係のインストールもしない。",
        "",
    ]
    if prev and prev[-1].get("findings"):
        lines += [f"## 前回（レビュー#{prev[-1]['n']}）の指摘（直っているか確認する）"]
        lines += [f"- [{f['label']}] {f['text']}" for f in prev[-1]["findings"]] + [""]
    li = last_impl(s)
    if li and li.get("code_fp") and li["code_fp"] != ctx.code_fp:
        lines += ["## 注意: 実装担当の終了後に、他の誰かがコードを変更している",
                  "- 変更内容を確認し、問題があれば指摘する。", ""]
    lines += [
        "## 最後の報告（この形式で書く。エンジンが読み取って記録する）",
        "```",
        "## レビュー結果",
        "判定: <合格 か 要修正 の1語だけ>",
        "重大な指摘: <数>",
        "軽微な指摘: <数>",
        f"レビュー対象: 仕様 {short(ctx.spec_hash)} / 設計 {short(ctx.design_hash)} / コード {short(ctx.code_fp)}",
        "### 指摘",
        "- [重大1] 区分: 実装|テスト|仕様|設計 / 対象: R1, AC1 / 対応区分: 修正必須 / 内容: … / 成立条件: … / "
        "影響: … / 根拠: ファイル:行・コードの経路・再現手順 / 提案: 最小の修正",
        "- [重大2] 区分: テスト / 対象: AC2, K1 / 対応区分: 証拠不足 / 内容: 足りない証拠 / 根拠: 必須である理由 / 提案: 必要な検証",
        "- [軽微1] 区分: … / 対象: … / 対応区分: 改善提案 / 内容: … / 提案: …",
        "### 受け入れ条件ごとの検証の妥当性",
        "- AC1: V1 は〜を確認しており妥当 / 〜のため不十分（受け入れ条件と重要な要求のリスクをすべて書く）",
        "### E2E の要否",
        "- 必要 / 不要（理由）",
        "### 不足しているリスク",
        "- R1: …（なければ「なし」）",
        "### 確認したこと",
        "- 実行したコマンドと結果",
        "```",
        "「受け入れ条件ごとの検証の妥当性」では、テストが通ることではなく、そのテストが受け入れ条件・リスクを"
        "本当に確かめているか（正常系だけでなく異常系・境界値、モックしすぎ、弱いアサーション）を評価する。",
        "「重大」の基準: 受け入れ条件の未達、重要な要求の検証不足・空洞化、設計との重大な不整合、重大なセキュリティの穴、"
        "テストの改ざん・弱体化。スタイルや好みは「軽微」。重大が1件でもあれば判定は「要修正」、0件なら「合格」。",
    ] + REVIEW_ACTION_RULES
    return lines


# ------------------------------------------------------------------ spawn gate
def spawn_problem(role, ctx):
    s = ctx.state
    if s.get("status") not in ("in_progress", "pr_ready"):
        return "実行中の run がありません。new で run を開始してください。"
    run = running_agents(s)
    if run:
        names = "、".join(ROLE_LABEL.get(v["role"], v["role"]) for v in run.values())
        return f"{names} が実行中です。サブエージェントは順番に実行します。完了を待ってください。"
    if role == "requirements":
        return None
    probs = problems(ctx)
    codes = [p["code"] for p in probs]
    if [p for p in probs if p["code"] == "memory_scope"]:
        return "Memory の利用範囲が未選択です。先に人間に確認して memory-scope を実行してください。"
    blk = [p for p in probs if p["code"] in ("config_changed", "state_invalid", "forged_human")]
    if blk:
        return blk[0]["msg"] + "。人間の確認が済むまで進めません。"
    from . import workflow as WF
    wf = WF.effective(s, ctx.cfg)
    if role == "design" and not wf["stages"]["design"]:
        return (f"この run の工程（{wf['mode']}）では設計担当を使いません（実装担当が構成を決め、チェックを登録します）。"
                "設計が必要なら workflow --set strict で工程を上げてください。")
    if role == "reviewer" and not wf["stages"]["review"]:
        return (f"この run の工程（{wf['mode']}）では独立レビューを使いません。"
                "レビューが必要なら workflow --set standard（または strict）で工程を上げてください。")
    spec_block = {"spec_missing", "graph_incomplete", "spec_unregistered", "questions_open",
                  "answers_unapplied", "spec_unpresented", "spec_unapproved", "dup_ids"}
    blocking = [p for p in probs if p["code"] in spec_block]
    if role == "design":
        if blocking:
            return "仕様が承認されていないため、設計に進めません: " + blocking[0]["msg"]
        return None
    if blocking:
        return "仕様が承認されていないため、進めません: " + blocking[0]["msg"]
    design_block = [p for p in probs if p["code"] in ("design_missing", "design_changed", "design_stale",
                                                      "design_unmapped", "no_checks", "escalation")]
    if role == "implementer":
        if design_block:
            return "設計が揃っていないため、実装に進めません: " + design_block[0]["msg"]
        return None
    if role == "reviewer":
        if design_block:
            return "設計が揃っていないため、レビューできません: " + design_block[0]["msg"]
        pre = [p for p in probs if p["code"] in ("impl_missing", "impl_stale", "impl_untraced", "verif_unbound",
                                                 "code_unowned", "violation_open", "review_fix", "gate_missing",
                                                 "gate_stale",
                                                 "gate_fail", "gate_modified", "cf_missing", "cf_stale",
                                                 "cf_survived", "cf_insufficient")]
        if pre:
            return "Quality Gate を通過した最新の実装がないため、レビューできません: " + pre[0]["msg"]
        return None
    return None


# ------------------------------------------------------------------ report parsing
# レビュー担当への、指摘の対応区分と判定の規則（agents/reviewer.md にも同じ文を書く。テストで食い違いを確かめる）
REVIEW_ACTION_RULES = [
    '#### 指摘の対応区分と判定',
    '- 指摘には「対応区分」を付ける。「合格」は指摘の種類ではなく、最後の判定。',
    '  - **修正必須** → [重大n]: 実在するバグ・仕様違反・認可・安全性・データ整合性の欠陥を、具体的に示せるもの。',
    '  - **証拠不足** → 必須の受け入れ条件・重要なリスクを確かめる検証がない・空洞化しているときだけ [重大n]。どの受け入れ条件・リスクのために必須かを書く。正しい実装の書き換えは求めず、提案はテスト・証拠の追加にする。必須でないテストの追加の勧めは「改善提案」。',
    '  - **改善提案** → [軽微n]: 動作と必須の検証は満たしているが、保守性・可読性などを良くできるもの。改善提案だけなら判定は「合格」（これだけで「要修正」にしない）。',
    '- 判定: [重大n] が1件でもあれば「要修正」、0件なら「合格」。「重大」は完了を止めるという意味で、欠陥の影響度（高・中・低）とは別。',
    '- 修正必須には、次を書く: 対象（ファイル・関数・行、要求・受け入れ条件の ID）、成立条件（どの入力・状態・並行処理・権限で起きるか）、影響（期待との違いと、利用者・データ・安全性への影響）、確認した保証（上流の検証・DB の制約・呼び出し元・既存のテストが防いでいないか）、根拠（コードの経路・仕様・設計・実行結果）、最小の修正案。',
    '- 証拠不足には、次を書く: 対象（必須の受け入れ条件・重要なリスクの ID）、内容（何の証拠が足りないか）、根拠（なぜ必須か）、提案（必要な検証）。',
    '- 取り下げた指摘は「### 指摘」に残さない。ラベルは種類ごとに1からの連番にし、見出しの件数と項目の数を合わせる。',
    '- エンジンは次の報告を受け付けず、書き直しを求める: 対応区分の欠落・不明な値・複数の値、対応区分とラベルの矛盾、件数の不一致、ラベルの重複・番号の飛び、必要な欄の欠落・空、重大0件の「要修正」、重大のある「合格」。欄があることだけを確かめ、内容の正しさは保証しない。',
    '- 再現の実行は必須ではない（コードと仕様から立証できる欠陥がある）。ただし、確かめていない仮説を断定しない。',
    '#### 誤検知を減らす確認',
    '- 1つのファイルだけで判断せず、呼び出し元から検証・認可・DB の操作・戻り値まで、必要な範囲をたどる。',
    '- 明示された仕様と既存のコードの決まりを優先し、好みや将来の仮定を欠陥として扱わない。',
    '- 上流で保証されている検証を、各層に重ねて求めない。',
    '- 「テストが薄い」を「実装に欠陥がある」と言い換えない。足りない検証と、それが必須である理由を分けて書く。',
    '- 同じ根本原因の指摘は1件にまとめる。',
    '- 分からない要件・到達経路は「未確認」と書く（証拠不足にするなら、なぜ必須かを示す）。',
    '#### 見逃しを防ぐ確認（減らさない）',
    '- 認証・認可、機密情報、データ整合性、並行処理、SQL の組み立ては、これまでどおり確かめる。',
    '- 確信度で一律に指摘を落とさない。必須の証拠の不足を「改善提案」に下げて合格にしない。',
]


# 対応区分（報告に書く語 → 記録の値）。[重大n] は修正必須・証拠不足、[軽微n] は改善提案だけ
ACTION_TYPES = {"修正必須": "fix_required", "証拠不足": "missing_evidence", "改善提案": "suggestion"}
ACTION_SEVERITY = {"修正必須": "重大", "証拠不足": "重大", "改善提案": "軽微"}
# 対応区分ごとに、指摘に書く欄（値が空でないこと）。欄があることだけを確かめる（内容の正しさは保証しない）
ACTION_FIELDS = {"修正必須": ("対象", "成立条件", "影響", "根拠", "提案"),
                 "証拠不足": ("対象", "内容", "根拠", "提案"),
                 "改善提案": ()}
_FIELD_HEAD = r"(?<![一-龥ぁ-んァ-ヶーA-Za-z0-9_])"


def _field_present(text, name):
    """「名前: 値」の欄があり、値が空でないか（値は次の「/」の手前まで）。"""
    # 「根拠（補足）: 値」のように、欄の名前の後に括弧書きがあってもよい
    m = re.search(_FIELD_HEAD + re.escape(name) + r"\s*(?:[（(][^）)]{0,80}[）)])?\s*\**\s*[:：]\s*\**\s*([^/／]*)",
                  text)
    return bool(m and m.group(1).strip(" *`…・-"))


def _action_of(text):
    """指摘の対応区分。戻り値: (報告の語 or None, 問題 or None)。"""
    vals = re.findall(r"対応区分\s*\**\s*[:：]\s*\**\s*([^/／\s*`]*)", text)
    if not vals:
        return None, "対応区分がない"
    if len(vals) > 1:
        return None, "対応区分が複数ある"
    v = vals[0].strip()
    if re.search(r"[|｜・、,，]", v) or sum(1 for k in ACTION_TYPES if k in v) > 1:
        return None, f"対応区分「{v}」に複数の値がある（1つだけ書く）"
    if v not in ACTION_TYPES:
        return None, f"対応区分「{v or '（空）'}」は不正（修正必須・証拠不足・改善提案のどれか）"
    return v, None


def parse_review(text):
    t = text or ""
    # 判定行は行全体で照合する（「合格 ではなく要修正」「合格 / 要修正」のような行は不正）
    lines = [ln for ln in t.splitlines() if re.match(r"^\s*[-*]?\s*\**\s*判定\s*\**\s*[:：]", ln)]
    values = []
    for ln in lines:
        v = re.sub(r"^\s*[-*]?\s*\**\s*判定\s*\**\s*[:：]", "", ln).strip().strip("*`").strip()
        values.append(v)
    raw_verdict = values[0] if values else None
    verdict = raw_verdict if raw_verdict in ("合格", "要修正") else None
    verdict_conflict = len(set(values)) > 1
    m = re.search(r"重大な指摘\s*[:：]\s*\**\s*(\d+)", t)
    head_block = int(m.group(1)) if m else None
    m = re.search(r"軽微な指摘\s*[:：]\s*\**\s*(\d+)", t)
    head_minor = int(m.group(1)) if m else None
    # 指摘は「### 指摘」の節（見出しが「指摘」だけのもの）の中だけを読む。前回の指摘への言及や、
    # 「確認したこと」に書いたラベルを、今回の指摘として数えない
    fsec = exact_section(t, "指摘")
    findings = []
    cur = None
    for ln in (fsec or "").splitlines():
        mm = re.match(r"^\s*(?:[-*・]|\d+[.)．])?\s*\**\s*[\[［【]\s*(重大|軽微)\s*(\d+)\s*[\]］】]\**\s*(.*)", ln)
        if mm:
            cur = {"severity": mm.group(1), "label": f"{mm.group(1)}{mm.group(2)}", "num": int(mm.group(2)),
                   "text": mm.group(3).strip()}
            c = re.search(r"(?<!対応)区分\s*[:：]\s*(実装|テスト|仕様|設計)", ln)
            cur["category"] = c.group(1) if c else "実装"
            tg = re.search(r"対象\s*[:：]\s*([^/／]+)", ln)
            cur["targets"] = [x.strip() for x in re.split(r"[,、\s]+", tg.group(1)) if x.strip()] if tg else []
            findings.append(cur)
        elif cur and ln.startswith((" ", "\t")) and ln.strip():
            cur["text"] += " " + ln.strip()
        elif ln.strip().startswith("#"):
            cur = None
    missing = bullet_items(section(t, "不足しているリスク"))
    # 「### 指摘」の節の中では、書き方の揺れに関係なく「重大n」のラベルを数える（安全側）
    labels_anywhere = set(re.findall(r"[\[［【]\s*重大\s*(\d+)\s*[\]］】]", fsec or ""))
    blocking_entries = max(sum(1 for f in findings if f["severity"] == "重大"), len(labels_anywhere))
    minor_entries = sum(1 for f in findings if f["severity"] == "軽微")
    errors = []
    if fsec is None:
        errors.append("「### 指摘」の節がない（指摘がなければ「- なし」と書く）")
    if raw_verdict is None:
        errors.append("「判定: 合格」または「判定: 要修正」の行がない")
    elif verdict is None:
        errors.append(f"判定「{raw_verdict}」は不正（行には「合格」か「要修正」の1語だけを書く）")
    if verdict_conflict:
        errors.append("判定の行が複数あり、値が一致しない")
    if head_block is None:
        errors.append("「重大な指摘: <数>」の行がない")
    if head_minor is None:
        errors.append("「軽微な指摘: <数>」の行がない")
    blocking = max(head_block or 0, blocking_entries)
    if head_block is not None and head_block != blocking_entries:
        errors.append(f"重大な指摘の数（{head_block}）と [重大n] の項目数（{blocking_entries}）が一致しない")
    if head_minor is not None and head_minor != minor_entries:
        errors.append(f"軽微な指摘の数（{head_minor}）と [軽微n] の項目数（{minor_entries}）が一致しない")
    minor = max(head_minor or 0, minor_entries)
    # ラベルの重複・番号の飛び（[重大1] が2回、[重大1]・[重大3] など）
    for sev in ("重大", "軽微"):
        nums = [f["num"] for f in findings if f["severity"] == sev]
        dups = sorted({n for n in nums if nums.count(n) > 1})
        if dups:
            errors.append("指摘のラベルが重複している: " + "、".join(f"[{sev}{n}]" for n in dups))
        elif nums and sorted(nums) != list(range(1, len(nums) + 1)):
            errors.append(f"[{sev}n] の番号が1からの連番になっていない（" + "、".join(str(n) for n in nums) + "）")
    # 対応区分: 欠落・不明・複数・ラベルとの矛盾・必要な欄の欠落（自動で補正せず、書き直しを求める）
    for f in findings:
        act, why = _action_of(f["text"])
        f["action_type"] = ACTION_TYPES.get(act)
        if why:
            errors.append(f"[{f['label']}] の{why}")
            continue
        if ACTION_SEVERITY[act] != f["severity"]:
            want = "修正必須・証拠不足" if f["severity"] == "重大" else "改善提案"
            errors.append(f"[{f['label']}] の対応区分「{act}」はラベルと矛盾している"
                          f"（[{f['severity']}n] は{want}。{act}なら [{ACTION_SEVERITY[act]}n] にする）")
            continue
        lack = [k for k in ACTION_FIELDS[act] if not _field_present(f["text"], k)]
        if lack:
            errors.append(f"[{f['label']}]（{act}）に必要な欄がない・空: " + "、".join(lack))
    # 判定と重大な指摘の数を完全に対応させる（重大1件以上 ⇔ 要修正、0件 ⇔ 合格）
    if verdict == "合格" and blocking > 0:
        errors.append(f"判定「合格」と重大な指摘 {blocking} 件が矛盾している（重大があれば「要修正」）")
    if verdict == "要修正" and blocking == 0:
        errors.append("判定「要修正」なのに重大な指摘が0件（改善提案だけなら「合格」。直すべきものは [重大n] で書く）")
    passed = verdict == "合格" and blocking == 0 and not errors
    return {"verdict": verdict, "raw_verdict": raw_verdict, "blocking": blocking, "minor": minor,
            "findings": findings, "missing_risks": missing, "passed": passed}, errors


# ------------------------------------------------------------------ validation at SubagentStop
def validate(role, ctx, entry, message):
    s, rd = ctx.state, ctx.rd
    errs = []
    if role == "requirements":
        md = read_text(doc_path(rd, "spec"))
        if md is None:
            errs.append(f"仕様書 {doc_path(rd, 'spec')} がない")
        else:
            mh = missing_headings(md, REQUIRED["requirements"])
            if mh:
                errs.append("仕様書に必須の見出しがない: " + "、".join(mh))
            ids = [r["id"] for r in s["requirements"]] + [a["id"] for r in s["requirements"] for a in r["acceptance"]]
            absent = [i for i in ids if i not in md]
            if absent:
                errs.append("仕様書の本文に要求・受け入れ条件のIDがない: " + "、".join(absent[:8]))
        if not s["requirements"]:
            errs.append("要求が登録されていない（req）")
        _i, dups = id_index(s)
        if dups:
            errs.append("IDが重複している: " + "、".join(dups))
        errs += graph_issues(s)
    elif role == "design":
        md = read_text(doc_path(rd, "design"))
        if md is None:
            errs.append(f"設計書 {doc_path(rd, 'design')} がない")
        else:
            mh = missing_headings(md, REQUIRED["design"])
            if mh:
                errs.append("設計書に必須の見出しがない: " + "、".join(mh))
            if not hash_line_ok(md, "仕様", ctx.spec_hash):
                errs.append(f"設計書に、基にした仕様の指紋の行（仕様: {short(ctx.spec_hash)}）がない、または古い")
        unmapped = [r["id"] for r in s["requirements"] if r["id"] not in s["design"].get("map", {})]
        if unmapped:
            errs.append("設計に対応付いていない要求（design-map）: " + "、".join(unmapped))
        if not ctx.checks:
            errs.append("Quality Gate のチェックが登録されていない（add-check）")
        from . import holdout as HO
        errs += HO.coverage_problems(s, ctx.cfg, ctx.project)
    elif role == "implementer":
        md = read_text(doc_path(rd, "impl"))
        if md is None or (entry.get("impl_hash_start") and doc_hash(rd, "impl") == entry.get("impl_hash_start")):
            errs.append(f"今回の実装報告 {doc_path(rd, 'impl')} が書かれていない")
        else:
            mh = missing_headings(md, REQUIRED["implementer"])
            if mh:
                errs.append("実装報告に必須の見出しがない: " + "、".join(mh))
            from . import workflow as WF
            # 設計担当を使う工程で、設計が登録済みなら、読んだ版の行を求める（実装中に工程が上がった場合は、
            # この時点では設計がないので求めない。次に設計担当が呼ばれ、実装は設計に合わせてやり直しになる）
            if WF.needs(s, ctx.cfg, "design") and s["design"].get("registered_hash") \
                    and not hash_line_ok(md, "設計書", ctx.design_hash):
                errs.append(f"実装報告の「設計書: {short(ctx.design_hash)}」の行がない、または古い版を参照している")
            if not hash_line_ok(md, "仕様", ctx.spec_hash):
                errs.append(f"実装報告の「仕様: {short(ctx.spec_hash)}」の行がない、または古い版を参照している")
            vfiles = sorted({f for v in open_violations(s) for f in v.get("files", [])})
            sec_all = section(md, "指摘への対応") or ""
            missv = [f for f in vfiles if f not in sec_all]
            if missv:
                errs.append("「## 指摘への対応」に、担当外の変更の記録があるファイルへの対応がない: " + "、".join(missv[:8]))
            fix = entry.get("fix") or {}
            if fix.get("labels"):
                sec = section(md, "指摘への対応") or ""
                miss = [lb for lb in fix["labels"] if lb not in sec]
                if miss:
                    errs.append("「## 指摘への対応」に次の項目がない: " + "、".join(miss))
        for r in s["requirements"]:
            files = s["impl"]["map"].get(r["id"]) or []
            if not files:
                errs.append(f"{r['id']} の実装ファイルが未登録（impl-map）")
            else:
                miss = [f for f in files if not os.path.exists(os.path.join(ctx.project, f))]
                if miss:
                    errs.append(f"{r['id']} の実装ファイルが存在しない: {', '.join(miss)}")
        unbound = [v["id"] for _r, _p, v in iter_verifications(s) if not G.is_automated(v) and not v.get("manual")]
        if unbound:
            errs.append("実行方法が未設定の検証（bind）: " + "、".join(unbound))
    elif role == "reviewer":
        rec, perrs = parse_review(message)
        errs += perrs
        # 証拠不足は、どの受け入れ条件・重要なリスクのための証拠かを対象に書く（その run にある ID）
        ev_ids = [a["id"] for r in s["requirements"] for a in r.get("acceptance", [])]
        ev_ids += [k["id"] for r in s["requirements"] for k in r.get("risks", [])]
        for f in rec["findings"]:
            if f.get("action_type") != "missing_evidence":
                continue
            tg = re.search(r"対象\s*[:：]\s*([^/／]*)", f["text"])
            tgt = tg.group(1) if tg else ""
            if ev_ids and not any(re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(i)}(?![0-9])", tgt) for i in ev_ids):
                errs.append(f"[{f['label']}]（証拠不足）の対象に、受け入れ条件・リスクの ID がない"
                            "（どの受け入れ条件・リスクのための証拠かを書く）")
        sec = section(message, "受け入れ条件ごとの検証の妥当性")
        if sec is None:
            errs.append("「### 受け入れ条件ごとの検証の妥当性」の節がない")
        else:
            need = [a["id"] for r in s["requirements"] for a in r.get("acceptance", [])]
            need += [k["id"] for r in s["requirements"] if r.get("critical") for k in r.get("risks", [])]
            miss = [i for i in need if not re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(i)}(?![A-Za-z0-9_.-])", sec)]
            if miss:
                errs.append("「受け入れ条件ごとの検証の妥当性」で評価していない受け入れ条件・重要なリスク: "
                            + "、".join(miss[:10]))
        e2e = section(message, "E2E の要否")
        if e2e is None or not e2e.strip():
            errs.append("「### E2E の要否」の節がない（必要か・不要ならその理由）")
        crec = CF.current(s)
        if crec and CF.cf_config(ctx.cfg)["mode"] != "off":
            surv = [m["id"] for m in crec.get("mutations", []) if m.get("result") == "survived"
                    and not m.get("equivalent")]
            miss = [i for i in surv if not re.search(rf"(?<![A-Za-z0-9]){i}(?![0-9])", message or "")]
            if miss:
                errs.append("反証テストで survived になった Mutation に触れていない: " + "、".join(miss[:10]))
        sig = entry.get("test_signals") or []
        unmentioned = [x["file"] for x in sig if x["file"] not in (message or "")]
        if unmentioned:
            errs.append("エンジンが検出したテストの兆候（skip・アサーションなしなど）に触れていないファイル: "
                        + "、".join(sorted(set(unmentioned))[:8]))
    return errs


def register(role, ctx, entry, message):
    """検証を通過した成果物を登録する（戻り値: 記録の要約）。"""
    s, rd = ctx.state, ctx.rd
    seq = bump(s)
    if role == "requirements":
        sp = s["spec"]
        sp["registered_hash"] = ctx.spec_hash
        sp["registered_seq"] = seq
        sp["version"] = int(sp.get("version", 0)) + 1
        sp["role_fp"] = entry.get("role_fp")
        return f"仕様 v{sp['version']}（{short(ctx.spec_hash)}）を登録"
    if role == "design":
        d = s["design"]
        d.update({"registered_hash": ctx.design_hash, "spec_hash": ctx.spec_hash, "registered_seq": seq,
                  "version": int(d.get("version", 0)) + 1, "role_fp": entry.get("role_fp")})
        _escalate(s, "design", read_text(doc_path(rd, "design")), seq)
        return f"設計 v{d['version']}（{short(ctx.design_hash)}）を登録"
    if role == "implementer":
        rounds = s["impl"]["rounds"]
        n = len(rounds) + 1
        fix = entry.get("fix") or {}
        for v in open_violations(s):
            v["resolved"] = {"by_round": n, "seq": seq}
        rec = {"n": n, "seq": seq, "at": now_iso(), "code_fp": ctx.code_fp, "snapshot": ctx.snap,
               "config_fp": ctx.config_fp, "design_hash": ctx.design_hash,
               "spec_hash": ctx.spec_hash, "report_hash": doc_hash(rd, "impl"), "fix": fix or None,
               "role_fp": entry.get("role_fp")}
        rounds.append(rec)
        if fix:
            s["fix_iterations"] = int(s.get("fix_iterations", 0)) + 1
        try:
            shutil.copyfile(doc_path(rd, "impl"), os.path.join(artifacts_dir(rd), f"impl-{n}.md"))
        except OSError:
            pass
        _escalate(s, "implementer", read_text(doc_path(rd, "impl")), seq)
        return f"実装 第{n}回（コード {short(ctx.code_fp)}）を登録"
    if role == "reviewer":
        rec, _ = parse_review(message)
        n = len(s["reviews"]) + 1
        path = os.path.join(artifacts_dir(rd), f"review-{n}.md")
        write_text(path, message or "")
        code_now = ctx.code_fp
        invalid = None
        if entry.get("code_fp_start") and entry["code_fp_start"] != code_now:
            invalid = "レビュー中にコードが変更された"
        elif entry.get("format_errors"):
            invalid = "報告の形式が不正（" + "、".join(entry["format_errors"][:2]) + "）"
        elif not entry.get("gate_ok_at_start"):
            invalid = "最新のコードで Quality Gate を通過していない状態でレビューが始まった"
        if invalid is None and rec.get("verdict") is None:
            invalid = "判定を読み取れない"
        rec["passed"] = bool(rec.get("passed")) and invalid is None
        rec.update({"n": n, "seq": seq, "at": now_iso(), "code_fp": code_now, "graph_fp": ctx.graph_fp,
                    "config_fp": ctx.config_fp,
                    "checks_fp": ctx.checks_fp, "spec_hash": ctx.spec_hash, "design_hash": ctx.design_hash,
                    "gate_stamp": (s.get("gate") or {}).get("stamp"), "report": os.path.relpath(path, ctx.project),
                    "role_fp": entry.get("role_fp"), "cf_fp": entry.get("cf_fp"),
                    "valid": invalid is None, "invalid_reason": invalid})
        s["reviews"].append(rec)
        if invalid:
            return f"レビュー#{n} は無効（{invalid}）"
        return f"レビュー#{n}: {rec['verdict']}（重大 {rec['blocking']} 件・軽微 {rec['minor']} 件）" + \
            ("" if rec["passed"] else "→ 完了不可（修正ループ）")
    return ""


def _escalate(s, src, md, seq):
    known = {(e.get("from"), e.get("text")) for e in s["escalations"]}
    for item in bullet_items(section(md, "差し戻し")):
        if (src, item) in known:
            continue
        n = len(s["escalations"]) + 1
        s["escalations"].append({"id": f"E{n}", "from": src, "text": item, "seq": seq, "status": "open"})


# ------------------------------------------------------------------ hook handlers
def on_pre_agent(payload, project, state, spath):
    ti = payload.get("tool_input") or {}
    role = role_of(ti.get("subagent_type"))
    if role not in ROLES:
        return None
    cfg = config(project)
    if not state:
        return deny("実行中の run がありません。E-AQDL の開発は new で run を開始してから行います。")
    ctx = Ctx(project, state, cfg, fresh=(role == "reviewer"))
    why = spawn_problem(role, ctx)
    if why:
        return deny(why)
    prompts = state["agents"].setdefault("prompts", {})
    prompts[role] = str(ti.get("prompt") or "")[:4000]
    save_state(spath, state)
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                  "permissionDecisionReason": f"E-AQDL: {ROLE_LABEL[role]} を順番に実行します"}}
    upd = None
    if cfg["automation"].get("force_foreground_agents", True) and ti.get("run_in_background") is not False:
        upd = dict(ti)
        upd["run_in_background"] = False
    model = str((cfg.get("review") or {}).get("model") or "").strip()
    if role == "reviewer" and model and ti.get("model") != model:
        # レビュー担当だけ別のモデルにする（実装担当と同じ盲点を共有しにくくする）
        upd = dict(upd or ti)
        upd["model"] = model
    used = (upd or ti).get("model") or "inherit"
    state["agents"].setdefault("models", {})[role] = used
    save_state(spath, state)
    if upd:
        out["hookSpecificOutput"]["updatedInput"] = upd
    return out


def on_subagent_start(payload, project, state, spath):
    role = role_of(payload.get("agent_type"))
    if role not in ROLES or not state:
        return None
    ctx = Ctx(project, state, fresh=True)
    aid = payload.get("agent_id") or f"{role}-{time.time()}"
    fresh, passing = gate_status(ctx)
    gate_ok = fresh and passing
    kind, items = pending_fix(ctx) if role == "implementer" else (None, [])
    signals, web = [], False
    if role == "reviewer":
        files = sorted(ctx.snap)
        signals = G.test_signals(project, files)
        web = G.looks_like_web_app(project, files)
    prompt = (state["agents"].get("prompts") or {}).get(role, "") if role == "requirements" else ""
    sel = role_selection(role, ctx, kind, prompt)
    sendback = bool(kind) or (role in ("requirements", "design") and any(
        p["code"] in ("review_fix", "escalation") for p in problems(ctx)))
    rrec = RL.record(state, role, sel, RL.trigger_of(role, state, sendback), state.get("seq", 0), aid)
    entry = {"role": role, "since": now_iso(), "epoch": time.time(), "start_seq": state.get("seq", 0),
             "code_fp_start": ctx.code_fp, "gate_ok_at_start": gate_ok, "blocks": 0,
             "impl_hash_start": doc_hash(ctx.rd, "impl") if role == "implementer" else None,
             "fix": {"kind": kind[0], "ref": kind[1], "labels": [f["label"] for f in items if not f.get("info")]} if kind else None,
             "test_signals": signals, "web_app": web, "role_fp": sel["fp"], "role_trigger": rrec["trigger"],
             "cf_fp": CF.record_fp(state) if role == "reviewer" else None}
    state["agents"]["running"][aid] = entry
    state["stop_blocks"] = 0
    save_state(spath, state)
    return {"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                   "additionalContext": brief(role, ctx, entry, sel)}}


def on_subagent_stop(payload, project, state, spath):
    role = role_of(payload.get("agent_type"))
    if role not in ROLES or not state:
        return None
    aid = payload.get("agent_id")
    running = state["agents"]["running"]
    entry = running.get(aid)
    if entry is None:
        cand = [k for k, v in running.items() if v.get("role") == role]
        aid = cand[-1] if cand else aid
        entry = running.get(aid, {"role": role, "since": now_iso(), "blocks": 0})
    message = payload.get("last_assistant_message") or _last_message(payload.get("agent_transcript_path"))
    ctx = Ctx(project, state, fresh=True)
    errs = validate(role, ctx, entry, message)
    max_blocks = int(config(project)["automation"].get("max_agent_stop_blocks", 3))
    if errs and entry.get("blocks", 0) < max_blocks and not (role == "reviewer" and _tainted(entry, ctx)):
        entry["blocks"] = entry.get("blocks", 0) + 1
        running[aid] = entry
        save_state(spath, state)
        reason = ("E-AQDL: 成果物が完成していないため、まだ終了できません。次を直してから終えてください:\n- "
                  + "\n- ".join(errs[:10]))
        return {"decision": "block", "reason": reason}
    running.pop(aid, None)
    msg_dir = os.path.join(artifacts_dir(ctx.rd), "messages")
    try:
        write_text(os.path.join(msg_dir, f"{state.get('seq', 0) + 1:04d}-{role}.md"), message or "")
    except OSError:
        pass
    if errs and role == "reviewer":
        entry["format_errors"] = errs
    if errs and role != "reviewer":
        summary = "成果物が不完全なまま終了（登録しない）: " + "、".join(errs[:3])
        ok = False
    else:
        summary = register(role, ctx, entry, message)
        ok = True
    state["agents"]["history"].append({"role": role, "agent_id": aid, "start": entry.get("since"),
                                       "end": now_iso(), "sec": round(time.time() - entry.get("epoch", time.time()), 1),
                                       "ok": ok, "summary": summary, "stop_blocks": entry.get("blocks", 0),
                                       "tokens": transcript_usage(payload.get("agent_transcript_path"))})
    state["stop_blocks"] = 0
    save_state(spath, state)
    return None


def _ts(v):
    """会話記録の timestamp（ISO 8601、Z 付きを含む）を UNIX 秒に。読めなければ None。"""
    if not v:
        return None
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def transcript_usage(path, since=None, until=None):
    """会話記録（JSONL）からトークン使用量を合計する（取得できなければ None）。

    since / until（ISO 8601）を渡すと、その期間の API 呼び出しだけを数える（同じ会話で複数の run を
    進めた場合に、別の run の分を混ぜないため）。時刻のない行は期間を判定できないので数え、件数を untimed に残す。
    同じ API 呼び出し（message.id）が複数行に分かれて記録されても1回として数える。
    数えるのは会話記録にある使用量だけで、料金には換算しない（課金区分はキーごとに分けて返す）。
    """
    if not path or not os.path.exists(path):
        return None
    import json
    t0, t1 = _ts(since), _ts(until)
    seen, models, untimed = set(), set(), 0
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except ValueError:
                    continue
                msg = obj.get("message") or {}
                u = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(u, dict):
                    continue
                t = _ts(obj.get("timestamp"))
                if t is not None and ((t0 is not None and t < t0) or (t1 is not None and t > t1)):
                    continue
                key = msg.get("id") or obj.get("uuid")
                if key in seen:
                    continue
                seen.add(key)
                if t is None and (t0 is not None or t1 is not None):
                    untimed += 1
                tot["input"] += int(u.get("input_tokens") or 0)
                tot["output"] += int(u.get("output_tokens") or 0)
                tot["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
                tot["cache_create"] += int(u.get("cache_creation_input_tokens") or 0)
                if msg.get("model"):
                    models.add(str(msg["model"]))
    except OSError:
        return None
    if not seen:
        return None
    tot["api_calls"] = len(seen)
    tot["models"] = sorted(models)
    if untimed:
        tot["untimed"] = untimed
    return tot


def _tainted(entry, ctx):
    return bool(entry.get("code_fp_start")) and entry["code_fp_start"] != ctx.code_fp


def _last_message(path):
    if not path or not os.path.exists(path):
        return ""
    import json
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except ValueError:
                    continue
                msg = obj.get("message") or {}
                if obj.get("type") == "assistant" or msg.get("role") == "assistant":
                    content = msg.get("content")
                    if isinstance(content, str):
                        last = content
                    elif isinstance(content, list):
                        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                        if texts:
                            last = "\n".join(texts)
    except OSError:
        return ""
    return last


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "E-AQDL: " + reason}}


# ------------------------------------------------------------------ permissions
ENGINE_RE = re.compile(r"aqdlc\.py[\"']?\s+[\"']?([a-z][a-z-]*)")
COMMON_READ = {"status", "next", "trace", "history", "show", "spec", "checks", "memory", "check", "escalation", "audit",
               "evidence", "roles",
               "metrics", "report", "workflow"}
ROLE_COMMANDS = {
    "requirements": COMMON_READ | {"req", "accept", "risk", "verify", "assume", "question", "drop"},
    "design": COMMON_READ | {"design-map", "decision", "add-check", "remove-check", "bind", "holdout"},
    "implementer": COMMON_READ | {"bind", "impl-map", "add-check", "gate", "cf"},
    "reviewer": COMMON_READ,
}
WORK_COMMANDS = {"req", "accept", "risk", "verify", "assume", "question", "drop", "design-map", "decision",
                 "add-check", "remove-check", "bind", "impl-map", "holdout"}
REVIEWER_BASH_DENY = re.compile(
    r"\bgit\s+(add|commit|push|checkout|switch|reset|stash|rebase|merge|restore|clean|rm|mv|apply|cherry-pick)\b"
    r"|(^|[\s;&|])(rm|mv|cp|touch|tee|truncate)\s|\bsed\s+-i|\bperl\s+-p?i"
    r"|\b(npm|pnpm|yarn|bun)\s+(i|install|add|remove|uninstall|update|upgrade)\b|\bpip3?\s+install\b"
    r"|--update-snapshots|\s-u\b.*\b(jest|vitest)\b|\b(jest|vitest)\b.*\s-u\b")


BRANCH_SWITCH = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(switch\b|checkout\s+(-b|-B|--orphan)\b|checkout\s+[^-\s.][^\s]*\s*$"
                           r"|branch\s+(?!--show-current|-a\b|-l\b|--list|-v|-vv|-r\b)\S)", re.M)


def check_bash(role, cmd, project, state, cfg):
    if not cfg["automation"].get("enforce_roles", True):
        return None
    if re.search(r"aqdlc\.py\s+(hook\b|[a-z-]+-hook\b)", _normalize(cmd)):
        return "hook 用のコマンドは手動で実行できません。"
    subs = ENGINE_RE.findall(cmd)
    for sub in subs:
        if role in ROLES:
            if sub not in ROLE_COMMANDS[role]:
                return (f"{ROLE_LABEL[role]} はエンジンの `{sub}` を使えません。"
                        "必要なら成果物（差し戻し・報告）に書いて統括に返してください。")
            if sub == "spec" and re.search(r"--(present|approve)", cmd):
                return "仕様の提示・承認は統括だけが行います。"
        elif role in ("orchestrator", "other") and sub in WORK_COMMANDS:
            owner = {"req": "requirements", "accept": "requirements", "risk": "requirements",
                     "verify": "requirements", "assume": "requirements", "question": "requirements",
                     "drop": "requirements", "design-map": "design", "decision": "design",
                     "add-check": "design", "remove-check": "design", "bind": "implementer", "holdout": "design",
                     "impl-map": "implementer"}[sub]
            return (f"`{sub}` は {ROLE_LABEL[owner]} の担当です。統括は直接編集せず、"
                    f"Agent ツールで aq-dlc:{owner} に依頼してください。")
    if role in ROLES and BRANCH_SWITCH.search(cmd):
        return "サブエージェントはブランチを作成・切り替えできません（統括が作った作業ブランチで作業する）。"
    if role == "reviewer" and REVIEWER_BASH_DENY.search(cmd):
        return "独立レビュー担当は、ファイルやリポジトリを変更するコマンドを実行できません。"
    from . import holdout as HO
    if role == "implementer" and HO.enabled(cfg) and re.search(r"holdout", _normalize(cmd), re.I) \
            and not re.search(r"aqdlc\.py\S*\s+holdout\s+--list\b", _normalize(cmd)):
        return "実装担当は隠し受け入れテスト（holdout）の中身を読めません。仕様と設計書の契約どおりに作ってください。"
    if role == "reviewer" and review_blind(cfg) and re.search(r"impl[^/\s]*\.md|artifacts/messages", cmd):
        return "ブラインドレビューのため、実装報告（impl.md）と実装担当のメッセージは読めません。"
    return None


def check_read(role, path, project, state, cfg):
    """Read・Grep・Glob の前の確認（隠し受け入れテスト・ブラインドレビュー）。"""
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(project, path)
    path = real_target(path)
    from . import holdout as HO
    if role == "implementer" and HO.enabled(cfg) and is_within(path, os.path.realpath(HO.root())):
        return "実装担当は隠し受け入れテスト（holdout）の中身を読めません。仕様と設計書の契約どおりに作ってください。"
    if role == "reviewer" and review_blind(cfg) and state:
        rd = run_dir(project, state.get("run_id"))
        if rd:
            arts = os.path.realpath(artifacts_dir(rd))
            if is_within(path, os.path.join(arts, "messages")) or \
                    (is_within(path, arts) and re.match(r"impl.*\.md$", os.path.basename(path))):
                return "ブラインドレビューのため、実装報告（impl.md）と実装担当のメッセージは読めません。"
    return None


ACTOR_TTL = 600


def caller_role(project, name):
    """直前の Bash（PreToolUse hook が記録）が、このエンジンのコマンドを含んでいれば、その担当を返す。

    変数や関数を経由した呼び出し（$AQ req ...）でも、Bash の本文にコマンド名が含まれるため判定できる。
    hook が動いていない（人が端末から直接使う）場合は None（制限しない）。
    """
    rd = run_dir(project)
    if not rd:
        return None
    a = load_json(os.path.join(rd, "actor.json"), {}) or {}
    if time.time() - a.get("at", 0) > ACTOR_TTL:
        return None
    if not re.search(rf"(^|[\s\"'`;(|&]){re.escape(name)}([\s\"'`;)|&]|$)", a.get("cmd", "")):
        return None
    return a.get("role")


def engine_policy(project, name, args, cfg):
    """エンジン側の権限確認。戻り値: 拒否の理由（None なら許可）。"""
    if not cfg["automation"].get("enforce_roles", True):
        return None
    role = caller_role(project, name)
    if role is None:
        return None
    if role in ROLES:
        if name not in ROLE_COMMANDS[role]:
            return f"{ROLE_LABEL[role]} はエンジンの `{name}` を使えません（担当外のコマンド）。"
        if name == "spec" and ("--present" in args or "--approve" in args):
            return f"{ROLE_LABEL[role]} は仕様の提示・承認ができません（統括の担当）。"
        return None
    if name in WORK_COMMANDS:
        return (f"`{name}` は統括の担当ではありません（担当のサブエージェントに Agent ツールで依頼してください）。")
    return None


def forged_hook(project, hook_name):
    """Bash から hook を直接呼んで、人間の返答やサブエージェントの終了を偽装していないか。"""
    rd = run_dir(project) if project else None
    if not rd:
        return False
    a = load_json(os.path.join(rd, "actor.json"), {}) or {}
    if time.time() - a.get("at", 0) > 60:
        return False
    cmd = _normalize(a.get("cmd", ""))
    return bool(re.search(r"\bhook\b", cmd) and hook_name in cmd)


def _normalize(cmd):
    """引用符・バックスラッシュで語を分ける書き方（ho""ok など）を詰めて判定する（完全ではない）。"""
    return re.sub(r"[\"'\\]", "", cmd or "")


def real_target(path):
    """存在しないファイルでも、存在する親までたどって実体のパスを求める。"""
    head, tail = os.path.abspath(path), []
    while head and not os.path.exists(head):
        head, t = os.path.split(head)
        if not t:
            break
        tail.append(t)
    return os.path.join(os.path.realpath(head), *reversed(tail)) if head else path


# ------------------------------------------------------------------ Bash の前後でのファイル変更の検出
def bash_snapshot_dir(project):
    """Bash の実行前の記録の置き場所。プロジェクトの外（~/.aq-dlc/tmp）に置き、Bash からの削除・改ざんを難しくする。"""
    import hashlib
    from .project import home
    key = hashlib.sha1(os.path.realpath(project).encode("utf-8")).hexdigest()[:16]
    return os.path.join(home(), "tmp", key)


def bash_human_path(project, tool_use_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_use_id or "last")[:80]
    return os.path.join(bash_snapshot_dir(project), f"human-{safe}.json")


def bash_snapshot_path(project, tool_use_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_use_id or "last")[:80]
    return os.path.join(bash_snapshot_dir(project), f"bash-{safe}.json")


GIT_SYNC = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(switch|checkout|pull|merge|rebase|reset|stash|restore|cherry-pick|revert|am)\b")


def detect_bash_changes(role, cmd, before, after, project):
    c, a, r = G.snapshot_diff(before, after)
    files = c + a + r
    if not files:
        return None
    return {"role": role, "at": now_iso(), "cmd": cmd[:500], "files": files[:50], "count": len(files),
            "kind": "git" if GIT_SYNC.search(cmd) else "write"}


def check_write(role, path, project, state, cfg):
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(project, path)
    auto = cfg["automation"]
    if role == "reviewer":
        return "独立レビュー担当はファイルを変更できません。"
    # シンボリックリンクをたどった実体で判定する（リンク経由でプロジェクト内を書き換えるのを防ぐ）
    path = real_target(path)
    project = os.path.realpath(project)
    from .project import home
    h = os.path.realpath(home())
    if is_within(path, h):
        from . import holdout as HO
        if state and HO.enabled(cfg) and \
                is_within(path, os.path.realpath(HO.run_holdout_dir(project, state.get("run_id")))):
            if role == "design" or not auto.get("enforce_roles", True):
                return None
            return "隠し受け入れテスト（holdout）は設計担当だけが書けます。"
        # 全体設定・実行中の記録は、エージェントからは変更できない（Memory は統括だけが書ける）
        if is_within(path, os.path.join(h, "memory")):
            return ("共通 Memory（~/.aq-dlc/memory）へは直接書き込めません。プロジェクトの .aq-dlc/memory に保存し、"
                    "共有する場合は memory-share で人間の承認を得てください（無断の横断共有を防ぐ）。")
        return "E-AQDL の全体設定・記録（~/.aq-dlc）はエージェントからは変更できません（人が直接編集する）。"
    if not is_within(path, project):
        return None
    rel = os.path.relpath(norm(path), norm(project)).replace(os.sep, "/")
    rd = run_dir(project, state.get("run_id")) if state else None
    arts = os.path.relpath(norm(artifacts_dir(rd)), norm(project)).replace(os.sep, "/") if rd else None
    base = os.path.basename(rel)
    if rel.startswith(".aq-dlc/"):
        if base in ("state.json", "config.json", "project.json", "actor.json", "ledger.jsonl", "audit.jsonl",
                    "CURRENT") or base.endswith(".lock"):
            return f"{base} は直接編集できません（エンジンが記録します）。"
        if rel.startswith(".aq-dlc/runs/") and base != "run.md" and not (arts and rel.startswith(arts + "/")):
            return "run の記録（gate・反証テストのログなど）はエンジンが書き込みます。"
        if arts and rel.startswith(arts + "/"):
            own = {"spec.md": "requirements", "design.md": "design", "impl.md": "implementer"}.get(rel[len(arts) + 1:])
            if own is None:
                return "この成果物はエンジンが書き込みます。"
            if role != own and auto.get("enforce_roles", True):
                return f"{rel[len(arts) + 1:]} は {ROLE_LABEL[own]} だけが書けます。"
            return None
        if role in ROLES and auto.get("enforce_roles", True):
            return "サブエージェントは自分の成果物以外の .aq-dlc 配下を変更できません。"
        return None
    if not state or state.get("status") not in ("in_progress", "pr_ready") or not auto.get("enforce_roles", True):
        return None
    if rel.startswith(".claude/"):
        if re.match(r"\.claude/(agents|skills|hooks|commands)/|\.claude/settings[^/]*\.json$", rel):
            return "run の実行中は .claude のエージェント定義・Skill・hook・設定を変更できません（担当の成りすましを防ぐ）。"
        return None if role == "orchestrator" else "サブエージェントは .claude 配下を変更できません。"
    if role == "implementer":
        return None
    if role in ("orchestrator", "other") and auto.get("orchestrator_edits_code", False):
        return None
    who = ROLE_LABEL.get(role, "統括")
    return (f"run の実行中は、コードやテストを書けるのは実装担当（aq-dlc:implementer）だけです（{who} は書けません）。"
            "修正が必要なら Agent ツールで aq-dlc:implementer に依頼してください。")
