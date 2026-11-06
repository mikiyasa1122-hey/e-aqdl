"""コマンドと hook の入口。"""
import datetime as _dt
import json
import locale
import os
import re
import shutil
import sys
import time

from . import agents as A
from . import gate as G
from . import approval as AP
from . import gitops, memory
from .project import (cmd_create, cmd_init, cmd_setup, config, project_config_path,
                      resolve_project, unregister_active)
from .state import (ROLE_LABEL, Ctx, StateError, bump, describe_state, ensure_new_id, find, id_index,
                    last_human_seq, load_state, log, new_run, next_action, problems,
                    req_by_id, run_dir, running_agents, save_state, trace_rows)
from .util import (die, info, load_json, now_iso, pop_flag, pop_opt, save_json, setup_stdio,
                   sh_run, split_list, write_text)
from .commands import open_state
from .commands.gate import cmd_gate
from .commands.metrics import cmd_metrics, collect_metrics
from .commands.spec import cmd_spec
from .hook_handlers import LEGACY_HOOKS, run_hook
from .hook_handlers.common import read_payload

USAGE = """E-AQDL エンジン（Evidence-driven Adaptive Quality Development Lifecycle / 旧 AQ-DLC）

準備:   setup / doctor / init ["<名前>"] / create "<名前>" / publish [--visibility V] [--dry-run] / new "<run名>"
要求:   req <ID> "<要求>" [--critical] [--kind nonfunctional] / accept <要求ID> <ID> "<受け入れ条件>"
        risk <要求ID> <ID> "<リスク>" / verify <受入ID|リスクID> <ID> "<確かめ方>" [--level L] [--manual "<手順>"] [--after-merge]
        question <ID> "<質問>" [--options "A（推奨）|B"] / assume "<内容>" [--out] / drop <ID>
設計:   design-map <要求ID> "<設計要素>" / decision "<判断>" / add-check <名前> "<コマンド>" --kind K [--needs a,b] / remove-check
実装:   bind <検証ID> [--command "..."] [--checks a,b] [--test "..."] [--level L] / impl-map <要求ID> --files a,b
統括:   next / status / spec [--present | --approve "<人間の返答の要約>"] / answer <質問ID> "<回答>"
        ask "<質問>" / resume / wait "<理由>" / confirm <検証ID> "<人間の確認内容>" / escalation [<ID> --resolve "<対応>"]
        gate [--after-merge] / cf [--plan] [--only M1] / cf-add <検証ID> --file F --line N --op OP
        cf-mark <M番号> --equivalent "<理由>" / ci [--wait 分] / complete / abort
Memory: memory-scope none|project|all / memory [--tags a,b] [--record]
        memory-use <ID> --result helped|not_applicable|harmful|unknown / memory-approve <ID>
        memory-share propose <ID> | decide | list
        memory-done --end project|share|none [--saved ID,..] [--merged ID,..] [--skipped "<理由>"]
参照:   evidence [--json] / roles [--role R] [--check] / trace / history [--since N] / show <ID> / checks / check / metrics [--json] / audit [--verify]
すべてのコマンドに --project <パス> を付けられる（省略時はカレントから探す）。"""


def mutate(args, fn):
    project, state, spath = open_state(args)
    if state.get("status") not in ("in_progress", "pr_ready"):
        die(f"この run は {state.get('status')} です。新しい開発は new で始めてください。")
    fn(project, state, args)
    save_state(spath, state)


def unique_id(state, prefix):
    idx, _ = id_index(state)
    n = 1
    while f"{prefix}{n}" in idx:
        n += 1
    return f"{prefix}{n}"


KIND_ALIASES = {"機能": "functional", "非機能": "nonfunctional", "functional": "functional",
                "nonfunctional": "nonfunctional", "non-functional": "nonfunctional"}


# ------------------------------------------------------------------ run
def cmd_new(args):
    force = pop_flag(args, "--force")
    project = resolve_project(args)
    try:
        cur, _ = load_state(project)
    except StateError as e:
        if not force:
            die(str(e))
        cur = None
    if cur and cur.get("status") in ("in_progress", "pr_ready") and not force:
        die(f"進行中の run があります: {cur['run_id']}（{cur.get('status')}）。complete か abort をしてから始めるか、"
            "--force を付けてください（進行中の run はそのまま残ります）。")
    title = " ".join(args).strip() or "run"
    rid, d = new_run(project, title)
    info(f"run を開始しました: {rid}\n  状態: {os.path.join(d, 'state.json')}\n  記録: {os.path.join(d, 'run.md')}")
    print("次: Agent ツールで aq-dlc:requirements を呼び、ユーザーの依頼文を渡す。")


# ------------------------------------------------------------------ requirements role
def cmd_req(args):
    critical = pop_flag(args, "--critical")
    kind = pop_opt(args, "--kind")

    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: req <ID> "<要求>" [--critical] [--kind nonfunctional]')
        rid, text = a[0], " ".join(a[1:])
        k = KIND_ALIASES.get(kind or "functional")
        if not k:
            die("--kind は functional / nonfunctional（機能 / 非機能）")
        node = ensure_new_id(s, rid, ("requirement", None))
        if node:
            before = dict(text=node["text"], critical=node.get("critical"), kind=node.get("kind"))
            node.update(text=text, critical=critical or node.get("critical", False), kind=kind and k or node.get("kind"))
            log(s, project, "req.update", {"id": rid, "before": before, "after": text})
        else:
            s["requirements"].append({"id": rid, "text": text, "kind": k, "critical": critical,
                                      "acceptance": [], "risks": []})
            log(s, project, "req.add", {"id": rid, "text": text})
        info(f"要求 {rid}{'（重要）' if critical else ''}{'（非機能）' if k == 'nonfunctional' else ''}: {text}")
    mutate(args, fn)


def _child(args, list_key, kind, usage):
    def fn(project, s, a):
        if len(a) < 3:
            die(usage)
        r = req_by_id(s, a[0])
        nid, text = a[1], " ".join(a[2:])
        node = ensure_new_id(s, nid, (kind, r["id"]))
        if node:
            log(s, project, f"{kind}.update", {"id": nid, "before": node["text"], "after": text})
            node["text"] = text
        else:
            r.setdefault(list_key, []).append({"id": nid, "text": text, "verifications": []})
            log(s, project, f"{kind}.add", {"id": nid, "parent": r["id"], "text": text})
        info(f"{'受け入れ条件' if kind == 'acceptance' else 'リスク'} {r['id']}/{nid}: {text}")
    mutate(args, fn)


def cmd_accept(args):
    _child(args, "acceptance", "acceptance", '使い方: accept <要求ID> <受け入れ条件ID> "<条件>"')


def cmd_risk(args):
    _child(args, "risks", "risk", '使い方: risk <要求ID> <リスクID> "<起こりうる失敗>"')


def _apply_vopts(v, o, project, s):
    changes = {}
    for key, val in o.items():
        if val is None:
            continue
        if key == "level" and val not in G.LEVELS:
            die(f"--level は {' / '.join(sorted(G.LEVELS))} のどれか")
        if key == "checks":
            val = split_list(val)
            cfg_names = {c["name"] for c in config(project).get("checks", [])}
            unknown = [n for n in val if n not in cfg_names]
            if unknown:
                die(f"登録されていないチェック: {', '.join(unknown)}（add-check で登録）")
        if key == "timeout_sec":
            val = int(val)
        if v.get(key) != val:
            changes[key] = {"before": v.get(key), "after": val}
            v[key] = val
    return changes


def cmd_verify(args):
    o = {"level": pop_opt(args, "--level"), "manual": pop_opt(args, "--manual"),
         "command": pop_opt(args, "--command"), "checks": pop_opt(args, "--checks"),
         "timeout_sec": pop_opt(args, "--timeout")}
    after = pop_flag(args, "--after-merge")

    def fn(project, s, a):
        if len(a) < 3:
            die('使い方: verify <受け入れ条件ID|リスクID> <検証ID> "<確かめ方>" [--level L] [--manual "..."] [--after-merge]')
        kind, parent, _ = find(s, a[0], ("acceptance", "risk"))
        vid, method = a[1], " ".join(a[2:])
        v = ensure_new_id(s, vid, ("verification", parent["id"]))
        new = v is None
        if new:
            v = {"id": vid, "method": method}
            parent.setdefault("verifications", []).append(v)
        elif v.get("method") != method:
            v["method"] = method
        if after:
            v["after_merge"] = True
        changes = _apply_vopts(v, o, project, s)
        log(s, project, "verify.add" if new else "verify.update", {"id": vid, "parent": parent["id"],
                                                                     "method": method, "changes": changes})
        info(f"検証 {parent['id']}/{vid}: {method}{'（マージ後）' if v.get('after_merge') else ''}")
    mutate(args, fn)


def cmd_question(args):
    options = pop_opt(args, "--options")

    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: question <ID> "<質問>" [--options "A（推奨）|B"]')
        qid, text = a[0], " ".join(a[1:])
        q = ensure_new_id(s, qid, ("question", None))
        opts = [x.strip() for x in (options or "").split("|") if x.strip()]
        from .decision_sheet import options_error
        err = options_error(opts)
        if err:
            die(f"選択肢を登録できません: {err}")
        if q:
            q.update(text=text, options=opts or q.get("options", []))
        else:
            s["questions"].append({"id": qid, "text": text, "options": opts, "status": "open", "seq": bump(s)})
        log(s, project, "question", {"id": qid, "text": text, "options": opts})
        info(f"質問 {qid}: {text}" + (f"（選択肢: {' / '.join(opts)}）" if opts else ""))
    mutate(args, fn)


def cmd_assume(args):
    out = pop_flag(args, "--out")

    def fn(project, s, a):
        text = " ".join(a).strip()
        if not text:
            die('使い方: assume "<内容>" [--out]')
        key, prefix = ("out_of_scope", "O") if out else ("assumptions", "A")
        nid = unique_id(s, prefix)
        s[key].append({"id": nid, "text": text})
        log(s, project, key, {"id": nid, "text": text})
        info(f"{'対象外' if out else '仮定'} {nid}: {text}")
    mutate(args, fn)


def cmd_drop(args):
    def fn(project, s, a):
        if not a:
            die("使い方: drop <ID>")
        kind, node, parent = find(s, a[0])
        if kind == "requirement":
            s["requirements"].remove(node)
            s["design"]["map"].pop(node["id"], None)
            s["impl"]["map"].pop(node["id"], None)
        elif kind in ("acceptance", "risk"):
            r = req_by_id(s, parent)
            r["acceptance" if kind == "acceptance" else "risks"].remove(node)
        elif kind == "verification":
            for r in s["requirements"]:
                for p in r.get("acceptance", []) + r.get("risks", []):
                    if node in p.get("verifications", []):
                        p["verifications"].remove(node)
        else:
            key = {"question": "questions", "assumption": "assumptions", "out_of_scope": "out_of_scope",
                   "decision": "decisions"}.get(kind)
            if not key:
                die(f"{kind} は drop できません。")
            s[key].remove(node)
        log(s, project, "drop", {"id": a[0], "kind": kind, "node": node})
        info(f"削除しました: {a[0]}（{kind}）。削除は履歴に残り、独立レビューで確認されます。")
    mutate(args, fn)


# ------------------------------------------------------------------ design role
def cmd_design_map(args):
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: design-map <要求ID> "<設計要素>"')
        req_by_id(s, a[0])
        s["design"]["map"][a[0]] = " ".join(a[1:])
        log(s, project, "design-map", {"id": a[0], "to": s["design"]["map"][a[0]]})
        info(f"設計の対応 {a[0]} → {s['design']['map'][a[0]]}")
    mutate(args, fn)


def cmd_decision(args):
    def fn(project, s, a):
        text = " ".join(a).strip()
        if not text:
            die('使い方: decision "<判断と理由>"')
        nid = unique_id(s, "D")
        s["decisions"].append({"id": nid, "text": text})
        log(s, project, "decision", {"id": nid, "text": text})
        info(f"設計判断 {nid}: {text}")
    mutate(args, fn)


def cmd_add_check(args):
    project = resolve_project(args)
    timeout = pop_opt(args, "--timeout")
    kind = pop_opt(args, "--kind") or "other"
    needs = split_list(pop_opt(args, "--needs"))
    if kind not in G.KINDS:
        die(f"--kind は {' / '.join(G.KIND_ORDER)} のどれか")
    if len(args) < 2:
        die('使い方: add-check <名前> "<コマンド>" --kind K [--needs a,b] [--timeout 秒]')
    name, command = args[0], " ".join(args[1:])
    if not re.match(r"^[A-Za-z0-9_.-]{1,40}$", name):
        die("チェック名は英数字と ._- の40文字以内")
    path = project_config_path(project)
    cfg = load_json(path, {}) or {}
    old = next((c for c in cfg.get("checks", []) if c.get("name") == name), None)
    entry = {"name": name, "command": command, "kind": kind}
    if needs:
        entry["needs"] = needs
    if timeout:
        entry["timeout_sec"] = int(timeout)
    cfg["checks"] = [c for c in cfg.get("checks", []) if c.get("name") != name] + [entry]
    state, spath = load_state(project)
    if state and state.get("config_fp_registered") and state["config_fp_registered"] != G.config_fp(config(project), project):
        die("設定がエンジンのコマンド以外で変更されています。先に人間の確認（config-accept）を済ませてください。")
    save_json(path, cfg)
    if state:
        state["config_fp_registered"] = G.config_fp(config(project), project)
        log(state, project, "check.update" if old else "check.add", {"name": name, "before": old, "after": entry})
        save_state(spath, state)
    info(f"チェックを登録しました: {name}（{kind}）→ {command}")


def cmd_remove_check(args):
    project = resolve_project(args)
    if not args:
        die("使い方: remove-check <名前>")
    path = project_config_path(project)
    cfg = load_json(path, {}) or {}
    old = next((c for c in cfg.get("checks", []) if c.get("name") == args[0]), None)
    cfg["checks"] = [c for c in cfg.get("checks", []) if c.get("name") != args[0]]
    state, spath = load_state(project)
    if state and state.get("config_fp_registered") and state["config_fp_registered"] != G.config_fp(config(project), project):
        die("設定がエンジンのコマンド以外で変更されています。先に人間の確認（config-accept）を済ませてください。")
    save_json(path, cfg)
    if state and old:
        state["config_fp_registered"] = G.config_fp(config(project), project)
        log(state, project, "check.remove", {"name": args[0], "before": old})
        save_state(spath, state)
    info(f"{'削除しました' if old else '見つかりませんでした'}: {args[0]}")


def cmd_checks(args):
    project = resolve_project(args)
    checks = config(project).get("checks", [])
    if not checks:
        print("チェックは登録されていません。")
    for c in G.ordered_checks(checks):
        need = G.implicit_needs(c, checks)
        print(f"{c['name']}（{c.get('kind', 'other')}）: {c['command']}" + (f"  前提: {', '.join(need)}" if need else ""))


# ------------------------------------------------------------------ implementer role
def cmd_bind(args):
    o = {"command": pop_opt(args, "--command"), "checks": pop_opt(args, "--checks"),
         "test": pop_opt(args, "--test"), "level": pop_opt(args, "--level"),
         "timeout_sec": pop_opt(args, "--timeout"), "manual": pop_opt(args, "--manual")}

    def fn(project, s, a):
        if not a or all(v is None for v in o.values()):
            die('使い方: bind <検証ID> [--command "..."] [--checks a,b] [--test "..."] [--level L]')
        _k, v, parent = find(s, a[0], ("verification",))
        changes = _apply_vopts(v, o, project, s)
        if changes:
            log(s, project, "bind", {"id": a[0], "parent": parent, "changes": changes})
        info(f"検証 {a[0]} を更新: " + ("、".join(f"{k}={c['after']}" for k, c in changes.items()) or "変更なし"))
    mutate(args, fn)


def cmd_holdout(args):
    """holdout <受け入れ条件ID> --command "<コマンド>" | <受け入れ条件ID> --remove | --list | --dir

    隠し受け入れテスト（設定 holdout.enabled が true のとき、設計担当が登録する）。コマンドの {holdout} は
    置き場所（プロジェクトの外）に置き換えて実行する。
    """
    from . import holdout as HO
    command = pop_opt(args, "--command")
    remove = pop_flag(args, "--remove")
    show_list = pop_flag(args, "--list")
    show_dir = pop_flag(args, "--dir")
    if show_list or show_dir:
        project, s, _ = open_state(args)
        d = HO.run_holdout_dir(project, s.get("run_id"))
        if show_dir:
            os.makedirs(d, exist_ok=True)
            print(d)
            return
        print(f"holdout: {'有効' if HO.enabled(config(project)) else '無効（設定 holdout.enabled）'}")
        for it in HO.items(s):
            print(f"- {it['ac']}: {it['command']}")
        if not HO.items(s):
            print("（登録なし）")
        return

    def fn(project, s, a):
        if not HO.enabled(config(project)):
            die("holdout は無効です（設定 holdout.enabled を true にすると使えます）。")
        if not a or (not command and not remove):
            die('使い方: holdout <受け入れ条件ID> --command "<コマンド（{holdout} は置き場所）>" | <ID> --remove')
        kind, _n, _p = find(s, a[0], ("acceptance",))
        if remove:
            if not HO.remove(s, a[0]):
                die(f"{a[0]} の holdout は登録されていません。")
            log(s, project, "holdout.remove", {"ac": a[0]})
            info(f"{a[0]} の holdout を外しました。")
            return
        HO.record(s, a[0], command)
        log(s, project, "holdout", {"ac": a[0], "command": command})
        info(f"{a[0]} の holdout を登録しました（gate で実行。実装担当には中身を見せない）。")
    mutate(args, fn)


def cmd_impl_map(args):
    files = split_list(pop_opt(args, "--files"))

    def fn(project, s, a):
        if not a or not files:
            die('使い方: impl-map <要求ID> --files "src/a.ts,src/b.ts"')
        req_by_id(s, a[0])
        rels = []
        for f in files:
            p = f if os.path.isabs(f) else os.path.join(project, f)
            if not os.path.exists(p):
                die(f"ファイルがありません: {f}")
            rels.append(os.path.relpath(p, project).replace(os.sep, "/"))
        s["impl"]["map"][a[0]] = rels
        log(s, project, "impl-map", {"id": a[0], "files": rels})
        info(f"実装の対応 {a[0]} → {', '.join(rels)}")
    mutate(args, fn)


def cmd_answer(args):
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: answer <質問ID> "<人間の回答>"')
        _k, q, _p = find(s, a[0], ("question",))
        if last_human_seq(s) <= q.get("seq", 0):
            die("この質問の後に人間の返答が記録されていません。AskUserQuestion で人間に聞いてから記録してください。")
        q.update(status="answered", answer=" ".join(a[1:]), answered_seq=bump(s), answered_at=now_iso())
        log(s, project, "answer", {"id": a[0], "answer": q["answer"]})
        info(f"回答を記録しました: {a[0]} → {q['answer']}")
    mutate(args, fn)


def cmd_ask(args):
    def fn(project, s, a):
        if not " ".join(a).strip():
            die('使い方: ask "<人間に確認すること>"（質問の内容を必ず書く）')
        s.update(awaiting_human=True, question=" ".join(a), asked_seq=bump(s))
        s["asks_total"] = int(s.get("asks_total", 0)) + 1
        info("人間への確認待ちとして記録しました（返答を得たら resume）。")
    mutate(args, fn)


def cmd_resume(args):
    def fn(project, s, a):
        s.update(awaiting_human=False, waiting=None)
        info("待機を解除しました。")
    mutate(args, fn)


def cmd_wait(args):
    minutes = int(pop_opt(args, "--minutes") or 30)

    def fn(project, s, a):
        s["waiting"] = {"for": " ".join(a) or "バックグラウンド処理", "since": now_iso(),
                        "until": time.time() + minutes * 60}
        info(f"待機中として記録しました（最大{minutes}分）: {s['waiting']['for']}")
    mutate(args, fn)


def cmd_confirm(args):
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: confirm <検証ID> "<人間の確認内容>"')
        _k, v, _p = find(s, a[0], ("verification",))
        if G.is_automated(v) and not v.get("after_merge"):
            die(f"{a[0]} は自動の検証です。gate で確かめてください。")
        if not s.get("asked_seq") or last_human_seq(s) <= s["asked_seq"]:
            die("ask の後に人間の返答が記録されていません。ask を実行して人間に確認してから記録してください。")
        target = f"VERIFY-{a[0]}"
        decision, ev, _f = AP.find_decision(s, target, s["asked_seq"])
        if decision != "approve":
            die(f"ask の後の人間の返答に、{a[0]} への明示的な確認（「承認 {target}」）がありません"
                + ("（却下の返答があります）" if decision == "reject" else "") + "。")
        AP.record(s, target, "approve", ev, run_dir(project, s.get("run_id")), " ".join(a[1:]), "confirm")
        v.update(confirmed_by="human", confirm_note=" ".join(a[1:]), confirmed_seq=bump(s))
        log(s, project, "confirm", {"id": a[0], "note": v["confirm_note"]})
        info(f"人間の確認として記録しました: {a[0]}")
    mutate(args, fn)


def cmd_defer(args):
    """defer <検証ID>[,<検証ID>...] "<人間の返答の要約>" — 人が確認できない検証を、人間の承認のもとで「未確認のまま」進める。

    合格の扱いにはしない（証拠の状態は insufficient のまま、完了報告と PR に未確認として残す）。
    対象は人が確認する検証（手動・マージ後の手動）だけ。自動の検証は対象外（検証条件を弱めないため）。
    """
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: defer <検証ID>[,<検証ID>...] "<人間の返答の要約>"')
        ids = [x for x in re.split(r"[,、\s]+", a[0]) if x]
        note = " ".join(a[1:]).strip()
        # 確認の起点: ask、または仕様の提示（判断シートで人の確認も一緒に聞く）のうち新しいほう
        base = max(int(s.get("asked_seq") or 0), int((s.get("spec") or {}).get("presented_seq") or 0))
        if not base or last_human_seq(s) <= base:
            die("ask（または仕様の提示）の後に人間の返答が記録されていません。人間に確認してから記録してください。")
        cfg = config(project)
        todo = []
        for vid in ids:
            _k, v, _p = find(s, vid, ("verification",))
            if G.is_automated(v):
                die(f"{vid} は自動の検証です。未確認のまま進める扱いは、人が確認する検証にだけ使えます"
                    "（自動の検証が環境の都合で動かないときは、仕様の変更として人間に確認する）。")
            if v.get("confirmed_by") == "human":
                die(f"{vid} はすでに人間の確認済みです。")
            target = f"DEFER-{vid}"
            decision, ev, _f = AP.find_decision(s, target, base)
            if decision != "approve":
                die(f"ask の後の人間の返答に、{vid} を未確認のまま進めることへの明示的な承認（「承認 {target}」）がありません"
                    + ("（却下の返答があります）" if decision == "reject" else "") + "。")
            todo.append((vid, v, target, ev))
        rd = run_dir(project, s.get("run_id"))
        for vid, v, target, ev in todo:
            AP.record(s, target, "approve", ev, rd, note, "defer")
            v["deferred"] = {"note": note, "seq": bump(s), "fp": G.verification_fp(v, cfg), "at": now_iso()}
            log(s, project, "defer", {"id": vid, "note": note})
        info("人間の判断で、未確認のまま進める検証として記録しました: " + ", ".join(i for i, *_ in todo)
             + "\n  合格の扱いにはなりません。完了報告と PR に「未確認」として必ず書いてください。")
    mutate(args, fn)


def cmd_config_accept(args):
    """config-accept "<人間の返答の要約>" — エンジン以外で変わった設定を、人間が確認したうえで受け入れる"""
    def fn(project, s, a):
        note = " ".join(a).strip()
        if not note:
            die('使い方: config-accept "<人間の返答の要約>"（ask で変更内容を人間に確認した後に実行する）')
        if not s.get("asked_seq") or last_human_seq(s) <= s["asked_seq"]:
            die("ask の後に人間の返答が記録されていません。設定の変更内容を ask で人間に確認してから実行してください。")
        new = G.config_fp(config(project), project)
        target = AP.target_id("CFG", new)
        decision, ev, _f = AP.find_decision(s, target, s["asked_seq"])
        if decision != "approve":
            die(f"ask の後の人間の返答に、この設定（{target}）への明示的な承認がありません"
                + ("（却下の返答があります）" if decision == "reject" else "")
                + f"。「承認 {target}」を選択肢に含めて確認してください。")
        AP.record(s, target, "approve", ev, run_dir(project, s.get("run_id")), note, "config")
        log(s, project, "config.accept", {"before": s.get("config_fp_registered"), "after": new, "note": note})
        s["config_fp_registered"] = new
        info("設定の変更を、人間の確認済みとして受け入れました（gate と独立レビューはやり直しになります）。")
    mutate(args, fn)


def cmd_escalation(args):
    resolve = pop_opt(args, "--resolve")
    project, s, spath = open_state(args)
    if not args:
        if not s["escalations"]:
            print("差し戻しはありません。")
        for e in s["escalations"]:
            print(f"{e['id']} [{'対応済み' if e.get('status') == 'resolved' else '未対応'}]（{e['from']}）{e['text']}"
                  + (f" → {e.get('resolution')}" if e.get("resolution") else ""))
        return
    if not resolve:
        die('使い方: escalation <ID> --resolve "<対応内容>"')
    _k, e, _p = find(s, args[0], ("escalation",))
    e.update(status="resolved", resolution=resolve, resolved_seq=bump(s))
    log(s, project, "escalation.resolve", {"id": args[0], "resolution": resolve})
    save_state(spath, s)
    info(f"差し戻し {args[0]} を対応済みにしました。")


# ------------------------------------------------------------------ Counterfactual Gate
def cmd_cf(args):
    """cf [--plan] [--only M1,M2] — 反証テスト（隔離したコピーに欠陥を入れ、テストが失敗するかを確かめる）"""
    from . import counterfactual as CF
    show_plan = pop_flag(args, "--plan")
    only = split_list(pop_opt(args, "--only"))
    project, s, spath = open_state(args)
    cfg = config(project)
    ctx = Ctx(project, s, cfg, fresh=True)
    cc = CF.cf_config(cfg)
    muts, notes = CF.plan(project, s, cfg, source=list(ctx.snap))
    if show_plan:
        print(f"反証テストの計画（mode: {cc['mode']}、上限 {cc['max_mutations']} 件、検証ごと {cc['per_verification']} 件）")
        for m in muts:
            print(f"  {m['id']} {m['verification']}（{m.get('risk') or m['parent']}）{m['file']}:{m['line']} "
                  f"{m['category']}\n      - {m['original'].strip()}\n      + {m['mutated'].strip()}")
        for n in notes:
            print(f"  注意: {n}")
        if not muts:
            print("  対象なし（自動の検証・impl-map の実装ファイル（Python / JS / TS）・適用できる箇所が必要）")
        return
    if cc["mode"] == "off":
        die("counterfactual.mode が off です（反証テストは無効）。使う場合は人間の確認のうえで .aq-dlc/config.json の "
            "counterfactual.mode を advisory か enforce にしてください（計画の確認だけなら cf --plan）。")
    if s.get("status") not in ("in_progress", "pr_ready"):
        die(f"この run は {s.get('status')} です。")
    from .state import actor_hint, gate_status
    actor = actor_hint(project)
    run = running_agents(s)
    if run and not (actor == "implementer" and all(v["role"] == "implementer" for v in run.values())):
        die("サブエージェントの実行中は反証テストを実行しません。完了を待ってください。")
    fresh, passing = gate_status(ctx)
    if not (fresh and passing):
        die("最新のコードで通常の Quality Gate に合格していません。反証テストは gate の合格後に実行します。")
    lock = os.path.join(ctx.rd, "cf.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3600:
        die("別の反証テストが実行中です（前回が異常終了した場合は数分後に再実行するか、cf.lock を人が削除する）。")
    write_text(lock, str(os.getpid()))
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        info(f"反証テスト {stamp}（{len(muts)} 件、mode: {cc['mode']}）")
        rec = CF.execute(project, s, cfg, muts, os.path.join(ctx.rd, "cf"), stamp, set(only) if only else None)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    s, spath = load_state(project)
    ctx2 = Ctx(project, s, cfg)
    rec["notes"] = notes + rec.get("notes", [])
    full, _n = CF.plan(project, s, cfg, source=list(ctx.snap), limit=10 ** 6)
    planned = {m["verification"] for m in muts}
    rec["unrun"] = sorted({m["verification"] for m in full} - planned)
    rec.update({"seq": bump(s), "checks_fp": ctx2.checks_fp, "graph_fp": ctx2.graph_fp,
                "def_fp": CF.definition_fp(s, cfg, muts), "env_fp": CF.env_fp(cfg), "partial": bool(only)})
    if only:
        rec["status"] = "partial" if rec["status"] == "done" else rec["status"]
        rec["notes"].append("一部だけ実行した（--only）。完了判定には全件の実行が必要")
    eqs = (s.get("counterfactual") or {}).get("equivalent", {})
    for m in rec["mutations"]:
        if CF.mutation_key(m) in eqs:
            m["equivalent"] = eqs[CF.mutation_key(m)]
    rec["summary"] = CF.summarize(rec)
    rec["fp"] = CF.fp_now(ctx2, muts)
    cfst = s.setdefault("counterfactual", {})
    cfst["latest"] = rec
    cfst.setdefault("runs", []).append({k: rec[k] for k in ("stamp", "at", "status", "summary", "mode")})
    cfst["runs"] = cfst["runs"][-20:]
    log(s, project, "counterfactual", {"stamp": stamp, "status": rec["status"], "summary": rec["summary"]})
    save_state(spath, s)
    c = rec["summary"]["counts"]
    print(f"結果: killed {c['killed']} / survived {c['survived']} / invalid {c['invalid']} / timeout {c['timeout']} / "
          f"対象外 {c['not_applicable']}（検出率 = killed ÷（killed＋survived）= "
          f"{rec['summary']['score'] if rec['summary']['score'] is not None else '算出不可'}、"
          f"母集団 {rec['summary']['valid']} 件、除外 {rec['summary']['excluded']} 件）")
    for n in rec["notes"]:
        print(f"  注意: {n}")
    if rec["status"] != "done":
        print(f"  ! この結果は使えません（{rec['status']}）")
    print("  通常の gate の合格とは別の結果です。survived は「テストが欠陥を見逃した」か「等価な変異」のどちらかです。")


def cmd_cf_add(args):
    """cf-add <検証ID> --file <パス> --line <行> --op <operator> — 反証テストの対象を追加する"""
    from . import mutation as MU
    rel, line, op = pop_opt(args, "--file"), pop_opt(args, "--line"), pop_opt(args, "--op")

    def fn(project, s, a):
        if not a or not rel or not line or not op:
            die(f"使い方: cf-add <検証ID> --file <パス> --line <行> --op <{'|'.join(MU.OPERATORS)}>")
        _k, v, _p = find(s, a[0], ("verification",))
        if op not in MU.OPERATORS:
            die(f"--op は {' / '.join(MU.OPERATORS)} のどれか")
        r = rel.replace("\\", "/")
        if os.path.isabs(r) or ".." in r.split("/"):
            die("--file はプロジェクト内の相対パスで指定してください")
        path = os.path.join(project, r)
        if os.path.islink(path) or not os.path.isfile(path):
            die(f"ファイルがない、またはシンボリックリンクです: {r}")
        if not MU.language(r):
            die("対応している言語は Python と JavaScript / TypeScript です")
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.read().splitlines(keepends=True)
        if not str(line).isdigit() or int(line) < 1:
            die("--line は 1 以上の整数で指定してください")
        n = int(line)
        if MU.apply(op, lines, n - 1, MU.language(r)) is None:
            die(f"{r}:{n} に {op} を適用できません（形式的な適用の検査で不可）")
        cfst = s.setdefault("counterfactual", {})
        tid = f"T{len(cfst.get('targets', [])) + 1}"
        cfst.setdefault("targets", []).append({"id": tid, "verification": a[0], "file": r, "line": n, "operator": op})
        log(s, project, "cf.add", {"id": tid, "verification": a[0], "file": r, "line": n, "operator": op})
        info(f"反証テストの対象 {tid} を追加しました: {a[0]} {r}:{n} {op}")
    mutate(args, fn)


def cmd_cf_mark(args):
    """cf-mark <M番号> --equivalent "<理由>" — survived の変異を等価として除外する（人間の承認が必要）"""
    from . import counterfactual as CF
    reason = pop_opt(args, "--equivalent")

    def fn(project, s, a):
        if not a or not reason:
            die('使い方: cf-mark <M番号> --equivalent "<等価と判断した理由>"（ask で人間の承認を得てから）')
        rec = CF.current(s)
        m = next((x for x in (rec or {}).get("mutations", []) if x["id"] == a[0]), None)
        if not m:
            die(f"最新の反証テストに {a[0]} がありません。")
        if m.get("result") != "survived":
            die(f"{a[0]} は survived ではありません（{m.get('result')}）。")
        target = f"EQ-{rec['stamp']}-{a[0]}"
        if not s.get("asked_seq"):
            die(f"ask で人間に確認してください（選択肢「等価として除外する（承認 {target}）」）。")
        decision, ev, _f = AP.find_decision(s, target, s["asked_seq"])
        if decision != "approve":
            die(f"ask の後の人間の返答に「承認 {target}」がありません。等価かどうかは人間が判断します。")
        AP.record(s, target, "approve", ev, run_dir(project, s.get("run_id")), reason, "equivalent")
        info_ = {"reason": reason, "approval": target, "at": now_iso()}
        s["counterfactual"].setdefault("equivalent", {})[CF.mutation_key(m)] = info_
        m["equivalent"] = info_
        rec["summary"] = CF.summarize(rec)
        log(s, project, "cf.equivalent", {"id": a[0], "reason": reason})
        info(f"{a[0]} を等価な変異として除外しました（検出率の母集団から外し、除外件数に数えます）。")
    mutate(args, fn)


# ------------------------------------------------------------------ Role Profile / Evidence
def cmd_roles(args):
    """roles [--role R] [--check] [--json] — 適用される Role Profile と理由"""
    from . import roles as RL
    check = pop_flag(args, "--check")
    as_json = pop_flag(args, "--json")
    role = pop_opt(args, "--role")
    project = resolve_project(args, required=False)
    cfg = config(project)
    profiles, rejected = RL.load_profiles(cfg)
    if check:
        print(f"Role Profile: {len(profiles)} 件（{RL.roles_dir()}）")
        for p in sorted(profiles.values(), key=lambda x: (x["kind"], x["id"])):
            print(f"  OK {p['kind']:6} {p['id']} v{p['version']} {p['hash'][:12]}  {p['title']}")
        for r in rejected:
            print(f"  NG {os.path.basename(r['path'])}: " + " / ".join(r["errors"]))
        if rejected:
            sys.exit(1)
        return
    s, _ = load_state(project) if project else (None, None)
    if not s:
        die("実行中の run がありません（Profile の検査だけなら roles --check）。")
    ctx = Ctx(project, s, cfg)
    out = {}
    for r in ([role] if role else ["requirements", "design", "implementer", "reviewer"]):
        kind, _items = A.pending_fix(ctx) if r == "implementer" else (None, [])
        out[r] = RL.public(A.role_selection(r, ctx, kind, (s["agents"].get("prompts") or {}).get(r, "")
                                            if r == "requirements" else ""))
    if as_json:
        print(json.dumps({"current": out, "history": (s.get("roles") or {}).get("history", [])},
                         ensure_ascii=False, indent=2))
        return
    for r, sel in out.items():
        print(f"■ {ROLE_LABEL.get(r, r)}")
        for x in sel["stage"]:
            print(f"  工程: {x['id']} v{x['version']}（{x['title']}）")
        for x in sel["domain"]:
            print(f"  追加: {x['id']} v{x['version']} 点数 {x['score']} — {' / '.join(x['reasons'])}"
                  + (f"（関係: {', '.join(x['related'][:6])}）" if x["related"] else ""))
        for x in sel["dropped"]:
            print(f"  見送り: {x['id']} — {x['why']}")
        if sel.get("rejected"):
            print("  ! 不正な Profile（使わない）: " + "、".join(x["path"] for x in sel["rejected"]))
    hist = (s.get("roles") or {}).get("history", [])
    if hist:
        print("■ 適用の履歴")
        for h in hist[-10:]:
            print(f"  #{h.get('seq')} {h.get('agent')}（{h.get('trigger')}）: "
                  + ", ".join([x["id"] for x in h.get("stage", [])] + [x["id"] for x in h.get("domain", [])]))


def cmd_evidence(args):
    """evidence [--json] — 検証ごとの証拠の状態（Negative Evidence Graph）"""
    from . import evidence as EV
    as_json = pop_flag(args, "--json")
    project, s, _ = open_state(args)
    ctx = Ctx(project, s)
    rows, nodes, counts = EV.summary(ctx)
    if as_json:
        print(json.dumps({"verifications": rows, "nodes": nodes, "counts": counts}, ensure_ascii=False, indent=2))
        return
    print("受け入れ条件・リスクごとの状態: " + "、".join(f"{EV.LABEL[k]} {v}" for k, v in counts.items()))
    print("| 要求 | 受入/リスク | 検証 | 種類 | 通常Gate | 反証テスト | レビュー | 状態 | 根拠 | 失効の原因 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        print("| " + " | ".join(str(c).replace("|", "/") for c in [
            x["requirement"] + ("【重要】" if x["critical"] else ""), x["parent"], x["verification"], x["kind"],
            x["gate"], x["counterfactual"], x["review"], f"{x['state']}（{EV.LABEL[x['state']]}）",
            "；".join(x["basis"]), "；".join(x["causes"])]) + " |")
    missing = [nid for nid, n in nodes.items() if not n["verifications"]]
    if missing:
        print("検証のない受け入れ条件・リスク: " + "、".join(missing))


def cmd_ci(args):
    wait_min = float(pop_opt(args, "--wait") or 8)
    project, s, spath = open_state(args)
    rec, msg = G.check_ci(project, wait_min)
    if rec is None:
        info(msg)
        return
    s, spath = load_state(project)
    s["ci"] = rec
    save_state(spath, s)
    G.print_ci(rec)
    if rec["result"] != "success":
        sys.exit(1)


def cmd_complete(args):
    project, s, spath = open_state(args)
    if running_agents(s):
        die("サブエージェントの実行中は完了にできません。")
    ctx = Ctx(project, s, fresh=True)
    probs = problems(ctx, "pr")
    if probs:
        print("[E-AQDL] まだ PR を出せる状態ではありません:\n  - " + "\n  - ".join(p["msg"] for p in probs))
        sys.exit(1)
    final = problems(ctx, "final")
    if final:
        s["status"] = "pr_ready"
        save_state(spath, s)
        print("[E-AQDL] PR を出せる状態です（status: pr_ready）。マージ後の確認が残っています:\n  - "
              + "\n  - ".join(p["msg"] for p in final)
              + "\n→ PR を作成して報告し、マージを待つ（この間 Stop hook は止めない）。"
                "\n→ マージ後: 既定ブランチを取り込み → gate --after-merge → 人の確認は ask でまとめて1回 → confirm → complete")
        return
    s["status"] = "complete"
    s["metrics"] = collect_metrics(s, project)
    save_state(spath, s)
    info("完了条件を満たしました。status を complete にしました。（運用の指標は metrics で確認）")
    from .state import deferred_list
    dl = deferred_list(s, ctx.cfg)
    if dl:
        info("ただし、人間の判断で未確認のまま進めた検証があります: " + ", ".join(dl)
             + "\n  完了報告では「完了（未確認あり）」とし、これらを確認していないことを必ず書いてください。")


def cmd_abort(args):
    project, s, spath = open_state(args)
    s["status"] = "aborted"
    save_state(spath, s)
    unregister_active(project)
    info("run を中断しました。")


def cmd_next(args):
    project, s, _ = open_state(args)
    phase, nxt, probs = next_action(Ctx(project, s))
    print(f"フェーズ: {phase}\n次: {nxt}")
    if len(probs) > 1:
        print("ほかの未充足:\n  - " + "\n  - ".join(p["msg"] for p in probs[1:6]))


def cmd_status(args):
    project = resolve_project(args)
    s, _ = load_state(project)
    print(f"プロジェクト: {project}")
    if not s:
        print("実行中の run はありません。")
        return
    ctx = Ctx(project, s)
    phase, nxt, probs = next_action(ctx)
    print(f"run: {s['run_id']}  状態: {s['status']}  フェーズ: {phase}")
    for ln in describe_state(ctx):
        print("  " + ln)
    if s.get("legacy"):
        print("  ! 旧形式（v0.4 以前）の run です。新しい run で始め直すことをおすすめします。")
    print(f"次: {nxt}")
    if probs:
        print("未充足:\n  - " + "\n  - ".join(p["msg"] for p in probs[:8]))


def cmd_check(args):
    project, s, _ = open_state(args)
    stage = "final" if s.get("status") == "pr_ready" else "pr"
    probs = problems(Ctx(project, s, fresh=True), stage)
    if probs:
        print("[E-AQDL] 完了条件を満たしていません:\n  - " + "\n  - ".join(p["msg"] for p in probs))
        sys.exit(1)
    info("完了条件 OK")


def cmd_trace(args):
    project, s, _ = open_state(args)
    from . import evidence as EV
    ctx = Ctx(project, s)
    rows = trace_rows(ctx)
    ev = {x["verification"]: x for x in EV.compute(ctx)}
    head = ["要求", "受入/リスク", "設計", "実装ファイル", "検証", "テスト", "結果", "gate", "反証テスト", "証拠の状態",
            "失効の原因"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        vid = str(r[4]).split("（")[0]
        x = ev.get(vid)
        extra = [x["counterfactual"], f"{x['state']}（{EV.LABEL[x['state']]}）", "；".join(x["causes"])] if x else \
            ["", "insufficient（証拠不足）", ""]
        print("| " + " | ".join(str(c).replace("|", "/") for c in list(r) + extra) + " |")


def cmd_workflow(args):
    """workflow [--set lite|standard|strict "<人間の返答の要約>"] — この run の工程（リスクに応じた選択）を表示・変更する

    上げるのは統括が自由に行える（理由を記録）。下げるのは、人間の「承認 MODE-<工程>-…」の返答があるときだけ。
    """
    from . import workflow as WF
    to = pop_opt(args, "--set")
    if not to:
        project, s, _ = open_state(args)
        cfg = config(project)
        e = WF.effective(s, cfg)
        print(WF.describe(s, cfg))
        print(f"設定 workflow.mode: {e['config']}  判定の出どころ: {e['source']}")
        return

    def fn(project, s, a):
        cfg = config(project)
        if A.caller_role(project, "workflow") in A.ROLES:
            die("工程の変更は統括だけが行えます（サブエージェントは差し戻しに書いて統括に返す）。")
        if to not in WF.MODES:
            die("使い方: workflow --set lite|standard|strict \"<人間の返答の要約、または上げる理由>\"")
        if WF.config_mode(cfg) == "full":
            die("設定 workflow.mode が full（従来どおり全工程）のため、工程は変えられません。")
        note = " ".join(a).strip()
        cur = WF.effective(s, cfg)["mode"]
        plan = s.setdefault("workflow_plan", {})
        if WF.ORDER.index(to) < WF.ORDER.index(cur):
            target = WF.mode_target(s, to)
            if not s.get("asked_seq") or last_human_seq(s) <= s["asked_seq"]:
                die(f"工程を下げるには人間の承認が必要です。ask を実行し、選択肢「{to} に下げる（承認 {target}）」を含めて確認してください。")
            decision, ev, _f = AP.find_decision(s, target, s["asked_seq"])
            if decision != "approve":
                die(f"ask の後の人間の返答に「承認 {target}」がありません（工程は下げません）。")
            AP.record(s, target, "approve", ev, run_dir(project, s.get("run_id")), note, "workflow")
        plan.update(human_mode=to, human_note=note or ("上げる" if WF.ORDER.index(to) > WF.ORDER.index(cur) else ""),
                    changed_at=now_iso())
        log(s, project, "workflow.set", {"from": cur, "to": to, "note": note})
        info(f"工程を {cur} から {to} にしました。")
    mutate(args, fn)


def cmd_report(args):
    """report — 人間向けの1ページの完了報告（証拠の状態・未確認・隠しテスト・時間・トークン）"""
    from . import evidence as EV
    project, s, _ = open_state(args)
    ctx = Ctx(project, s)
    rows = EV.compute(ctx)
    m = collect_metrics(s, project)
    groups = [("証明済み", lambda x: x["state"] == "verified"),
              ("合格・反証は未実施", lambda x: x["state"] == "passed_but_unchallenged"),
              ("反証は対象外", lambda x: x["state"] == "not_applicable"),
              ("未確認（人の判断で進めた）", lambda x: x["gate"] == "未確認（人の判断で保留）"),
              ("証拠不足", lambda x: x["state"] == "insufficient" and x["gate"] != "未確認（人の判断で保留）"),
              ("失効（再確認が必要）", lambda x: x["state"] == "invalidated")]
    print(f"# 完了報告: {s.get('title')}（{s['run_id']}・状態 {s['status']}）")
    from . import workflow as WF
    if WF.config_mode(ctx.cfg) != "full":
        print(WF.describe(s, ctx.cfg))
    print()
    print("## 証拠の状態")
    for label, f in groups:
        hit = [x for x in rows if f(x)]
        if hit:
            print(f"- **{label}**: {len(hit)} 件 — " + "、".join(f"{x['verification']}（{x['parent']}）" for x in hit[:12])
                  + (" …" if len(hit) > 12 else ""))
    g = s.get("gate") or {}
    ho = (g.get("results") or {}).get("holdout") or {}
    if ho:
        ok = [ac for ac, r in ho.items() if r.get("pass")]
        ng = [ac for ac, r in ho.items() if not r.get("pass")]
        print(f"- **隠し受け入れテスト**: 合格 {len(ok)} / {len(ho)}" + (f"（不合格: {', '.join(ng)}）" if ng else ""))
    print()
    print("## 進め方の記録")
    print(f"- 時間: {m['run_sec'] // 60} 分 {m['run_sec'] % 60} 秒。Quality Gate {m['gate']['runs']} 回（不合格 {m['gate']['failed']}）、"
          f"独立レビュー {m['reviews']['runs']} 回（合格 {m['reviews']['passed']}）、修正ループ {m['fix_iterations']} 回")
    print(f"- 人間への確認: {m['human']['asks']} 回、承認 {m['approvals']['approve']} 件")
    t = m["tokens_total"]
    print("- トークン: " + ("取得できず" if not t else
                         f"入力（キャッシュ含む）{t['input'] + t['cache_read'] + t['cache_create']:,}・出力 {t['output']:,}"
                         + ("" if m["tokens_complete"] else "（一部取得できず、下限）")) + "（料金ではない）")
    models = (s.get("agents") or {}).get("models") or {}
    if models.get("reviewer"):
        print(f"- レビュー担当のモデル: {models['reviewer']}")
    print()
    print("通常の gate の合格は「テストが欠陥を検出できることの証明」ではありません。未確認・証拠不足の項目は、確かめていない範囲です。")


def cmd_history(args):
    since = int(pop_opt(args, "--since") or 0)
    project, s, _ = open_state(args)
    for h in s["history"]:
        if h["seq"] > since:
            print(f"#{h['seq']} {h['at']} [{h['actor']}] {h['action']} {json.dumps(h['detail'], ensure_ascii=False)}")


def cmd_show(args):
    project, s, _ = open_state(args)
    if not args:
        die("使い方: show <ID>")
    kind, node, parent = find(s, args[0])
    print(json.dumps({"kind": kind, "parent": parent, **node}, ensure_ascii=False, indent=2))


def cmd_audit(args):
    """audit [--verify] [--limit N] — 承認・同意・共有の決定、hook の異常、偽装の疑いの解決（--resolve FORGED-n）、状態の保存拒否の監査ログ"""
    verify = pop_flag(args, "--verify")
    resolve = pop_opt(args, "--resolve")
    limit = int(pop_opt(args, "--limit") or 30)
    if resolve:
        def fn(project, s, a):
            f = next((x for x in s.get("forged_events", []) if x["id"] == resolve), None)
            if not f:
                die(f"{resolve} が見つかりません。")
            if not s.get("asked_seq"):
                die(f"ask で人間に状況を報告し、「承認 {resolve}」の返答を得てから実行してください。")
            decision, ev, _f = AP.find_decision(s, resolve, s["asked_seq"])
            if decision != "approve":
                die(f"ask の後の人間の返答に「承認 {resolve}」がありません。")
            AP.record(s, resolve, "approve", ev, run_dir(project, s.get("run_id")), "偽装の疑いを人間が確認", "forged")
            f.update(resolved=True, resolved_at=now_iso())
            info(f"{resolve} を人間の確認済みにしました。取り消された承認は取り直してください。")
        return mutate(args, fn)
    from .project import home
    project = resolve_project(args, required=False)
    paths = []
    if project:
        s, _ = load_state(project)
        rd = run_dir(project, s.get("run_id")) if s else None
        if rd:
            paths.append(("この run", os.path.join(rd, "audit.jsonl")))
    paths.append(("全体（hook の異常・共通 Memory の共有）", os.path.join(home(), "audit.jsonl")))
    paths.append(("共通 Memory", os.path.join(home(), "memory", "audit.jsonl")))
    bad = False
    for label, path in paths:
        n, errs = AP.audit_verify(path)
        print(f"■ {label}: {path}（{n} 行）")
        if verify:
            if errs:
                bad = True
                print("  ! 連鎖の不一致: " + " / ".join(errs[:5]))
            else:
                print("  連鎖の確認: 不一致なし（ファイルごと作り直す改ざんは検出できない）")
        for rec in AP.audit_read(path, limit):
            detail = {k: v for k, v in rec.items() if k not in ("at", "event")}
            print(f"  {rec.get('at')} {rec.get('event')} {json.dumps(detail, ensure_ascii=False)[:300]}")
    if bad:
        sys.exit(1)


def cmd_doctor(args):
    project = resolve_project(args, required=False) or os.getcwd()
    ok = True

    def line(good, msg, fix=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK ' if good else 'NG '} {msg}" + (f"\n       → {fix}" if fix and not good else ""))
    info("環境の確認")
    line(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}", "Python 3.8 以上を入れてください")
    has_git = shutil.which("git") is not None
    line(has_git, "git", "git をインストールしてください")
    if has_git:
        for k in ("user.name", "user.email"):
            code, out = sh_run(f"git config --get {k}", project, 10)
            line(code == 0 and bool(out.strip()), f"git {k}: {out.strip() or '未設定'}",
                 f'git config --global {k} "<値>" を実行してください')
    has_gh = shutil.which("gh") is not None
    line(has_gh, "gh（GitHub CLI）", "GitHub 連携を使う場合はインストールして gh auth login を実行してください")
    if has_gh:
        line(sh_run("gh auth status", project, 20)[0] == 0, "gh のログイン", "gh auth login を実行してください")
    enc = locale.getpreferredencoding(False)
    print(f"  情報 OS の文字コード: {enc}（子プロセスの出力は UTF-8 → {enc} → cp932 の順で読む）")
    if sys.platform == "win32":
        print("  情報 Windows: Playwright はプロジェクト内にブラウザを入れる設定（PLAYWRIGHT_BROWSERS_PATH=0）を推奨。"
              "init 時に gate.env に設定済み。")
        if "WindowsApps" in sys.executable:
            print("  注意 Microsoft Store 版の Python です。AppData への書き込みが別の場所に振り替えられることがあります。")
    info("問題は見つかりませんでした。" if ok else "NG の項目を直すと、途中で止まりにくくなります。")


# ------------------------------------------------------------------ main
COMMANDS = {
    "setup": cmd_setup, "init": cmd_init, "create": cmd_create, "doctor": cmd_doctor,
    "publish": gitops.cmd_publish, "new": cmd_new,
    "req": cmd_req, "accept": cmd_accept, "risk": cmd_risk, "verify": cmd_verify, "question": cmd_question,
    "assume": cmd_assume, "drop": cmd_drop,
    "design-map": cmd_design_map, "decision": cmd_decision, "add-check": cmd_add_check,
    "remove-check": cmd_remove_check, "checks": cmd_checks,
    "bind": cmd_bind, "impl-map": cmd_impl_map, "holdout": cmd_holdout, "report": cmd_report, "workflow": cmd_workflow,
    "spec": cmd_spec, "answer": cmd_answer, "ask": cmd_ask, "resume": cmd_resume, "wait": cmd_wait,
    "confirm": cmd_confirm, "defer": cmd_defer, "escalation": cmd_escalation, "gate": cmd_gate, "ci": cmd_ci,
    "memory": memory.cmd_memory, "memory-done": memory.cmd_memory_done, "memory-use": memory.cmd_memory_use,
    "memory-scope": memory.cmd_memory_scope, "memory-approve": memory.cmd_memory_approve,
    "memory-share": memory.cmd_memory_share, "complete": cmd_complete, "abort": cmd_abort,
    "next": cmd_next, "status": cmd_status, "check": cmd_check, "trace": cmd_trace, "history": cmd_history,
    "show": cmd_show, "metrics": cmd_metrics, "config-accept": cmd_config_accept, "audit": cmd_audit,
    "cf": cmd_cf, "cf-add": cmd_cf_add, "cf-mark": cmd_cf_mark, "roles": cmd_roles, "evidence": cmd_evidence,
}
REMOVED = {"review": "レビュー結果は aq-dlc:reviewer の終了時にエンジンが自動で記録します（手動では記録できません）。",
           "phase": "フェーズは状態から自動で決まります（next で確認）。",
           "risk-only": ""}


def main(argv=None):
    setup_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    name, rest = argv[0], argv[1:]
    if name == "hook":
        run_hook(rest[0] if rest else "")
        return
    if name in LEGACY_HOOKS:
        if LEGACY_HOOKS[name]:
            run_hook(LEGACY_HOOKS[name])
        else:
            read_payload()
        return
    if name in REMOVED:
        die(REMOVED[name])
    if name not in COMMANDS:
        print(USAGE)
        sys.exit(2)
    if name not in ("setup", "init", "create", "doctor", "help"):
        peek = list(rest)
        project = resolve_project(peek, required=False)
        if project:
            why = A.engine_policy(project, name, rest, config(project))
            if why:
                die(why)
    try:
        COMMANDS[name](rest)
    except StateError as e:
        die(str(e))
