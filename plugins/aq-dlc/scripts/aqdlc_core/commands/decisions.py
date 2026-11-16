"""設計方針の選択（DEC）のコマンド。規則と表示は design_decisions.py。

- dec          要求担当・設計担当: 論点と2〜3案を登録する（未決定。提示の後は変えられない）
- dec-select   統括: 人の返答（「DEC-001 = B」）を確かめて、選んだ案を記録する
- dec-revisit  設計担当・実装担当・統括: 前提が崩れたので、再確認に戻す（選択を無言で変えない）
- decisions    だれでも: 採用した方針・未決定・履歴を表示する（--pending で未決定だけ）
"""
from .. import approval as AP
from .. import design_decisions as DD
from ..state import actor_hint, bump, log, save_state
from ..util import die, info, now_iso, pop_flag, pop_opt
from . import open_state


def _mutable(args):
    project, s, spath = open_state(args)
    if s.get("status") not in ("in_progress", "pr_ready"):
        die(f"この run は {s.get('status')} です。新しい開発は new で始めてください。")
    s.setdefault("design_decisions", [])
    return project, s, spath


def _pop_all(args, name):
    out = []
    while name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            die(f"{name} の値がない")
        out.append(args[i + 1])
        del args[i:i + 2]
    return out


def cmd_dec(args):
    usage = ('使い方: dec DEC-001 "<論点>" --option "A|<方針>|<利点>|<欠点>|<向いている条件>" --option "B|…" '
             '[--option "C|…"] --recommend A --affects R1,R2 (--minimal A | --minimal-rejected "<理由>") '
             '[--assume "<推奨の前提>"] [--supersedes DEC-00x]')
    raw_opts = _pop_all(args, "--option")
    rec = (pop_opt(args, "--recommend") or "").upper() or None
    affects = [a.strip() for a in (pop_opt(args, "--affects") or "").split(",") if a.strip()]
    minimal = (pop_opt(args, "--minimal") or "").upper() or None
    minimal_rejected = pop_opt(args, "--minimal-rejected")
    assume = pop_opt(args, "--assume")
    supersedes = pop_opt(args, "--supersedes")
    project, s, spath = _mutable(args)
    if len(args) < 2:
        die(usage)
    did, topic = args[0], " ".join(args[1:]).strip()
    options = []
    for t in raw_opts:
        o, err = DD.parse_option(t)
        if err:
            die("登録できません: " + err)
        options.append(o)
    options.sort(key=lambda o: o["key"])
    old = DD.get(s, did)
    if old and old.get("status") == "selected":
        die(f"{did} は決定済みです（{old.get('selected')}）。変えるなら dec-revisit で再確認に戻してから、人に選び直してもらう。")
    if old and old.get("status") == "superseded":
        die(f"{did} は置き換え済みです。新しい ID で登録してください。")
    role = actor_hint(project)
    origin = old.get("origin") if old else ("requirements" if role in ("requirements", "orchestrator") else role)
    d = {"id": did, "topic": topic, "options": options, "recommended": rec, "affects": affects,
         "assumptions": assume or "", "minimal": minimal, "minimal_rejected": minimal_rejected or "",
         "origin": origin, "status": "proposed", "selected": None, "history": (old or {}).get("history", []),
         "seq": (old or {}).get("seq") or bump(s), "updated_seq": bump(s)}
    req_ids = {r["id"] for r in s.get("requirements", [])}
    errs = DD.validate(d, req_ids)
    if supersedes:
        tgt = DD.get(s, supersedes)
        if not tgt or tgt is old:
            errs.append(f"置き換える DEC（{supersedes}）がない")
    if not old and len(DD.pending(s)) >= DD.MAX_OPEN:
        errs.append(f"未決定の DEC が {DD.MAX_OPEN} 件あります。重要な分岐だけを人に聞く（命名や自明な実装の詳細は聞かない）")
    if errs:
        die("登録できません:\n  - " + "\n  - ".join(errs) + "\n" + usage)
    if old:
        old.clear()
        old.update(d)
    else:
        s["design_decisions"].append(d)
    if supersedes:
        tgt = DD.get(s, supersedes)
        tgt.update(status="superseded", superseded_by=did, superseded_seq=bump(s))
    log(s, project, "dec", {"id": did, "topic": topic, "options": [o["key"] for o in options], "recommended": rec,
                            "origin": origin, "supersedes": supersedes})
    save_state(spath, s)
    info(f"設計方針の論点 {did}（{origin}）を登録しました（未決定）: {topic}。推奨 {rec}。"
         + ("仕様の提示に載り、承認の返答で決まります。" if origin == "requirements"
            else "人の選択（dec-select）が済むまで、設計・実装・レビューに進めません。統括に返してください。"))


def cmd_dec_select(args):
    reason = pop_opt(args, "--reason") or ""
    project, s, spath = _mutable(args)
    if len(args) != 2:
        die('使い方: dec-select DEC-001 B --reason "<人が選んだ理由（返答の要約）>"')
    did, key = args[0], args[1].upper()
    d = DD.get(s, did)
    if not d or d.get("status") == "superseded":
        die(f"{did} がない、または置き換え済みです。")
    keys = [o["key"] for o in d.get("options", [])]
    if key not in keys:
        die(f"{did} の案は {' / '.join(keys)} です。")
    after = max(d.get("updated_seq", 0), (d.get("revisit") or {}).get("seq", 0), d.get("decided_seq", 0))
    ev = None
    for e in reversed([e for e in s.get("human_events", []) if e.get("seq", 0) > after and e.get("parsed", True)]):
        got, why = DD.choice_in(e.get("text", ""), did, keys)
        if got is None and why is None:
            continue
        if why:
            die(f"人の返答が曖昧なため記録しません: {why}。「{did} = {' / '.join(keys)}」の形で確認し直してください。")
        if s.get("eaqdl") and not AP.in_transcript(s, e):
            continue
        ev = (e, got)
        break
    if not ev:
        die(f"この論点の提示の後の人の返答に、{did} の選択（「{did} = {key}」）がありません。"
            f"`decisions --pending` の表を人に見せて選んでもらい、返答を得てから記録してください。")
    e, got = ev
    if got != key:
        die(f"人の返答は {did} = {got} です（指定は {key}）。返答どおりに記録してください。")
    _select(s, d, key, "human", reason, e)
    log(s, project, "dec.select", {"id": did, "selected": key, "by": "human", "event_seq": e.get("seq")})
    save_state(spath, s)
    info(f"{did} = {key}（{DD.selected_option(d)['title']}）を記録しました。"
         + ("前の選択は履歴に残しました。設計・実装は、この方針で更新が必要です（next に従う）。" if d["history"] else ""))


def _select(s, d, key, by, reason, event=None):
    if d.get("selected") and d.get("selected") != key:
        d.setdefault("history", []).append({"selected": d["selected"], "decided_by": d.get("decided_by"),
                                            "at": d.get("decided_at"), "status": "superseded"})
    d.update(status="selected", selected=key, decided_by=by, reason=reason, decided_seq=bump(s), decided_at=now_iso(),
             decided_event=(event or {}).get("seq"))
    d.pop("revisit", None)


def apply_spec_approval(s, event_text):
    """仕様の承認で、要求担当の未決定の DEC を決める（cmd_spec から呼ぶ）。

    返答に「DEC-001 = B」があればその案（人の選択）、なければ推奨（仕様の承認）。曖昧なら (None, 理由) を返し、
    呼び出し側は承認を記録しない。戻り値: (決めた [(id, key, by)], 曖昧の理由の列)。
    """
    plan, why = [], []
    for d in DD.pending(s):
        if d.get("origin") != "requirements":
            continue
        keys = [o["key"] for o in d.get("options", [])]
        got, w = DD.choice_in(event_text or "", d["id"], keys)
        if w:
            why.append(w)
        plan.append((d, got or d["recommended"], "human" if got else "spec_approval"))
    if why:
        return None, why
    done = []
    for d, key, by in plan:
        _select(s, d, key, by, "仕様の承認の返答" if by == "human" else "推奨（仕様の承認）")
        done.append((d["id"], key, by))
    return done, []


def cmd_dec_revisit(args):
    project, s, spath = _mutable(args)
    if len(args) < 2:
        die('使い方: dec-revisit DEC-001 "<前提が崩れた事実・新しい事実>"')
    did, why = args[0], " ".join(args[1:]).strip()
    d = DD.get(s, did)
    if not d or d.get("status") != "selected":
        die(f"{did} は決定済みではありません（再確認に戻せるのは決定済みの DEC）。")
    d["status"] = "proposed"
    d["revisit"] = {"by": actor_hint(project), "reason": why, "seq": bump(s), "at": now_iso(),
                    "previous": d.get("selected")}
    log(s, project, "dec.revisit", {"id": did, "reason": why})
    save_state(spath, s)
    info(f"{did} を再確認に戻しました（今の選択: {d.get('selected')}）。人の選択（dec-select）が済むまで、"
         "設計・実装・レビューに進めません。")


def cmd_decisions(args):
    only_pending = pop_flag(args, "--pending")
    _project, s, _ = open_state(args)
    if only_pending:
        pend = DD.pending(s)
        if not pend:
            print("未決定の設計方針はありません。")
            return
        for d in pend:
            print("\n".join(DD.table_lines(d)) + "\n")
        print("人には、上の表と「" + " / ".join(f"{d['id']} = …" for d in pend) + "」の形の返答を求める。"
              "返答を得たら dec-select で記録する。")
        return
    print(DD.summary_markdown(s), end="")
