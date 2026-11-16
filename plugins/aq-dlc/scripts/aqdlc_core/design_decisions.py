"""設計方針の選択（DEC）: 実装上の重要な分岐を、AI が一案に決め打ちせず、人が選んで仕様の一部として固定する。

正本は run の state の design_decisions（この一覧）。仕様の確認シート・`decisions` の出力・各担当の引き継ぎは、
ここから作る表示で、二重に管理しない。設計書（design.md）は、採用した DEC の ID を書いて参照する。

流れ
- 要求担当が `dec` で論点と2〜3案（推奨1つ・現状踏襲／最小変更の検討を含む）を登録する（status: proposed）。
- 仕様の提示に表として載り、仕様の承認と同じ1回の返答で決まる。返答に「DEC-001 = B」があればその案、
  なければ推奨の案（selected）。要求担当の DEC は仕様の指紋に入る（提示の後に変えたら、提示し直す）。
  選んだ案は仕様の指紋に入れない（推奨以外を選んでも、承認した仕様はそのまま有効にするため）。
- 設計担当・実装担当は、前提が崩れたら `dec-revisit` で再確認に戻す（status: proposed に戻る）。設計担当は
  新しい論点を `dec` で足せる。未決定（proposed）の DEC がある間は、設計・実装・レビューに進めない。
- 人の返答で `dec-select` すると選び直せる。前の選択は履歴に superseded として残る。選択が変わると
  設計方針の指紋（decisions_fp）が変わり、設計・実装（とその後の gate・レビュー）が古くなる。
- `dec ... --supersedes DEC-001` で、新しい論点に置き換えた古い DEC は status: superseded になる。

このモジュールは状態を書き換えない純粋関数だけを持つ（cli と state_policy・agents から使う）。
"""
import re
import unicodedata

from .util import fingerprint

STATUSES = ("proposed", "selected", "superseded")
KEYS = ("A", "B", "C")
ID_RE = re.compile(r"^DEC-\d{3}$")
FIELDS = ("title", "pros", "cons", "fits")
FIELD_LABEL = {"title": "方針", "pros": "利点", "cons": "欠点", "fits": "向いている条件"}
MAX_OPEN = 3  # 同時に未決定にできる数（単純な修正で選択の質問を大量に出さないため）


def all_decs(state):
    return state.get("design_decisions") or []


def get(state, did):
    return next((d for d in all_decs(state) if d.get("id") == did), None)


def active(state):
    return [d for d in all_decs(state) if d.get("status") != "superseded"]


def pending(state):
    return [d for d in all_decs(state) if d.get("status") == "proposed"]


def selected_option(d):
    return next((o for o in d.get("options", []) if o["key"] == d.get("selected")), None)


def parse_option(text):
    """「A|方針|利点|欠点|向いている条件」→ dict。問題があれば (None, 理由)。"""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) != 5:
        return None, f"選択肢は「キー|方針|利点|欠点|向いている条件」の5つの欄で書く（{len(parts)} 欄ある）: {text[:60]}"
    key = parts[0].upper()
    if key not in KEYS:
        return None, f"選択肢のキーは A・B・C のどれか（{parts[0]}）"
    empty = [FIELD_LABEL[f] for f, v in zip(FIELDS, parts[1:]) if not v]
    if empty:
        return None, f"選択肢 {key} の欄が空: " + "、".join(empty)
    return dict(zip(("key",) + FIELDS, [key] + parts[1:])), None


def validate(d, requirement_ids):
    """登録の前の検査。問題の一覧（空なら登録できる）。"""
    errs = []
    if not ID_RE.match(d.get("id") or ""):
        errs.append("ID は DEC-001 の形（DEC- と3桁の数字）")
    if not (d.get("topic") or "").strip():
        errs.append("論点がない")
    opts = d.get("options") or []
    keys = [o["key"] for o in opts]
    if not 2 <= len(opts) <= 3:
        errs.append(f"選択肢は2〜3案（{len(opts)} 案ある。差が実質的にない案を数合わせで作らない）")
    if len(set(keys)) != len(keys):
        errs.append("選択肢のキーが重複している")
    if sorted(keys) != list(KEYS[:len(keys)]):
        errs.append("選択肢のキーは A から順に付ける（A・B・C）")
    if d.get("recommended") not in keys:
        errs.append("推奨（--recommend）は選択肢のキーのどれか1つ")
    if not d.get("minimal") and not (d.get("minimal_rejected") or "").strip():
        errs.append("現状踏襲・最小変更の案を検討する: その案のキーを --minimal で示すか、"
                    "その案を採らない理由を --minimal-rejected で書く")
    if d.get("minimal") and d.get("minimal") not in keys:
        errs.append("--minimal は選択肢のキーのどれか")
    aff = d.get("affects") or []
    if not aff:
        errs.append("影響する要求（--affects R1,R2）を1つ以上書く")
    bad = [a for a in aff if a not in requirement_ids]
    if bad:
        errs.append("存在しない要求: " + "、".join(bad))
    return errs


def spec_items(state):
    """仕様の指紋に入れる値: 要求担当が出した DEC（論点・選択肢・推奨・影響）。選んだ案は入れない。

    該当がなければ空（DEC を使わない run の仕様の指紋を変えない）。
    """
    out = []
    for d in all_decs(state):
        if d.get("origin") != "requirements":
            continue  # 置き換えた DEC も元の内容のまま入れる（設計担当の置き換えで、承認済みの仕様を無効にしない）
        out.append([d["id"], d.get("topic"), [[o["key"]] + [o.get(f) for f in FIELDS] for o in d.get("options", [])],
                    d.get("recommended"), sorted(d.get("affects") or []), d.get("assumptions") or ""])
    return out


def decisions_fp(state):
    """採用した設計方針の指紋（設計・実装が前提にしたもの）。選び直す・再確認に戻すと変わる。"""
    return fingerprint(sorted([d["id"], d.get("status"), d.get("selected")] for d in all_decs(state)))


# ------------------------------------------------------------------ 人の返答から選択を読む
_DIFFERENT = re.compile(r"ではなく|じゃなく|でなく|以外|違う|変え|変更|やめ|保留|未定|迷|けど|けれど|やっぱり|やはり|いや|でも|"
                        r"\bNOT\b|\bINSTEAD\b")


def normalize(text):
    """全角・半角、大文字・小文字、ダッシュの揺れをそろえる（ＤＥＣ－００１ → DEC-001、dec-001 → DEC-001）。"""
    t = unicodedata.normalize("NFKC", text or "").upper()
    return re.sub(r"[‐‑‒–—―ー−－]", "-", t)


def _id_re(did):
    m = re.match(r"DEC-(\d+)$", did)
    num = str(int(m.group(1))) if m else re.escape(did)
    return rf"DEC\s*-\s*0*{num}(?![0-9])"


def mentions(text, did):
    return re.search(_id_re(did), normalize(text)) is not None


def choice_in(text, did, keys, recommended=None):
    """返答の中の「DEC-001 = B」「DEC-001: B」「DEC-001 は B」。戻り値: (キー or None, 曖昧の理由 or None)。

    表記の揺れ（全角・小文字・DEC-1）はそろえて読む。引用の行（> で始まる）は読まない。次のどれかなら曖昧とする
    （推奨で上書きしない）: 同じ DEC に違うキーがある、案の並び（A / B / C）を書き写している、否定・言い直しの言葉
    （ではなく・けど・やっぱり など）が同じ文にある、推奨と言いながら推奨以外の案を書いている、DEC に触れているのに
    キーを読めない。
    """
    t = "\n".join(ln for ln in normalize(text).splitlines() if not ln.lstrip().startswith(">"))
    idre = _id_re(did)
    if not re.search(idre, t):
        return None, None
    # その DEC に触れた文と、その後ろ（別の DEC に触れるまで）。「DEC-001 = B。いや、やっぱり推奨で」のような言い直しを読む
    near = []
    for m in re.finditer(idre, t):
        nxt = re.search(r"DEC\s*-\s*\d+", t[m.end():])
        near.append(t[max(0, t.rfind("\n", 0, m.start()) + 1):m.end() + (nxt.start() if nxt else len(t) - m.end())])
    if any(re.search(rf"{idre}\s*(?:=|:|は|を|→)?\s*[A-C]\s*/\s*[A-C]", seg) for seg in near):
        return None, f"{did} の案の並び（A / B …）が書き写されていて、どれを選んだか読めない"
    pat = re.compile(rf"{idre}\s*(?:=|:|は|を|→)\s*(?:案\s*)?([A-C])(?![A-Z0-9])")
    found = {m.group(1) for m in pat.finditer(t)}
    if len(found) > 1:
        return None, f"{did} に複数の案（{'・'.join(sorted(found))}）が書かれている"
    if found:
        k = found.pop()
        if k not in keys:
            return None, f"{did} の案 {k} はない（{'・'.join(keys)} のどれか）"
        if any(_DIFFERENT.search(seg) for seg in near):
            return None, f"{did} の選択に、否定・言い直し・保留の言葉が一緒に書かれている"
        if k != recommended and any("推奨" in seg for seg in near):
            return None, f"{did} に「推奨」と書きながら、推奨（{recommended or '?'}）以外の案 {k} がある"
        return k, None
    return None, f"{did} に触れているが、選んだ案（{did} = {' / '.join(keys)}）を読めない"


def title_conflicts(texts, d):
    """返答の中に、推奨以外の案の名前（方針）があるか（ID を書かずに、選択肢の名前で答えた場合）。"""
    flat = normalize("\n".join(texts))
    flat = re.sub(r"\s+", "", flat)
    out = []
    for o in d.get("options", []):
        name = re.sub(r"\s+", "", normalize(o.get("title")))
        if o["key"] != d.get("recommended") and len(name) >= 4 and name in flat:
            out.append(f"{d['id']}: 推奨以外の案の名前（{o['key']}: {o['title']}）が返答にある")
    return out


# ------------------------------------------------------------------ 表示
def table_lines(d, show_choice=True):
    rec = d.get("recommended")
    lines = [f"### {d['id']}: {d.get('topic')}", "",
             "| 選択 | 方針 | 利点 | 欠点 | 向いている条件 |", "| --- | --- | --- | --- | --- |"]
    for o in d.get("options", []):
        mark = "（推奨）" if o["key"] == rec else ""
        mark += "（現状踏襲・最小変更）" if o["key"] == d.get("minimal") else ""
        lines.append(f"| {o['key']}{mark} | {o['title']} | {o['pros']} | {o['cons']} | {o['fits']} |")
    lines.append("")
    lines.append(f"推奨: {rec}" + (f"（前提: {d['assumptions']}）" if d.get("assumptions") else ""))
    if d.get("minimal_rejected"):
        lines.append(f"現状踏襲・最小変更の案を採らない理由: {d['minimal_rejected']}")
    lines.append(f"影響する要求: {', '.join(d.get('affects') or [])}")
    if d.get("revisit"):
        lines.append(f"再確認の理由（{d['revisit'].get('by')}）: {d['revisit'].get('reason')}")
    if show_choice:
        lines.append(f"確認したい選択: {d['id']} = {' / '.join(o['key'] for o in d.get('options', []))}")
    return lines


def adopted_lines(state):
    """採用した設計方針と、採用しない代替案（設計・実装・レビューの引き継ぎで使う制約）。"""
    out = []
    for d in all_decs(state):
        if d.get("status") != "selected":
            continue
        o = selected_option(d) or {}
        others = [x for x in d.get("options", []) if x["key"] != d.get("selected")]
        out.append(f"- {d['id']} {d.get('topic')} → **{d.get('selected')}: {o.get('title')}**"
                   f"（決定: {'人の選択' if d.get('decided_by') == 'human' else '仕様の承認（推奨）'}"
                   + (f"・理由: {d['reason']}" if d.get("reason") else "") + "）")
        if d.get("assumptions"):
            out.append(f"  - 前提: {d['assumptions']}")
        for x in others:
            out.append(f"  - 採用しない: {x['key']}: {x['title']}（欠点として挙げたこと: {x['cons']}）")
    return out


def summary_markdown(state):
    lines = ["# 設計方針の決定（DEC）", ""]
    sel = adopted_lines(state)
    lines += ["## 採用する設計方針"] + (sel or ["- なし"]) + [""]
    pend = pending(state)
    if pend:
        lines += ["## 未決定（人の選択が必要）", ""]
        for d in pend:
            lines += table_lines(d) + [""]
    sup = [d for d in all_decs(state) if d.get("status") == "superseded"]
    if sup:
        lines += ["## 置き換えた論点"] + [f"- {d['id']} {d.get('topic')}（→ {d.get('superseded_by') or '?'}）"
                                         for d in sup] + [""]
    hist = [(d["id"], h) for d in all_decs(state) for h in d.get("history", [])]
    if hist:
        lines += ["## 選び直しの履歴"] + [f"- {i}: {h.get('selected')}（{h.get('status')}・{h.get('at')}）"
                                       for i, h in hist] + [""]
    return "\n".join(lines).rstrip() + "\n"
