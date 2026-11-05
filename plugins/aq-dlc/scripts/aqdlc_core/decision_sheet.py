"""判断シート: 推奨のある質問を、仕様の承認と同じ1回の返答で決めるための規則（純粋関数）。

state.py（状態の検証・保存）と cli.py（承認の記録）の両方から使う。ここは状態を書き換えず、
パッケージ内の他のモジュールにも依存しない（循環を作らないため）。

- 推奨は、選択肢に付いた「（推奨）」「(推奨)」の印だけで決める。「非推奨」や本文中の「推奨」は推奨ではない。
- 推奨の印が2つ以上ある質問は登録できない。既存のデータにあれば「推奨なし」として扱う（先に人に聞く）。
- 提示した推奨は仕様の指紋に入る。提示の後に推奨や質問を変えたら、提示し直さないと承認できない。
- 承認の返答が、推奨以外を選んでいる・推奨を否定している・質問に触れているのに推奨どおりと言っていない、
  のどれかなら、承認として扱わない（推奨と違う回答を、推奨で上書きしないため）。
"""
import re

MARKER_RE = re.compile(r"[（(]\s*推奨\s*[)）]")
# 質問 ID に触れたうえで、推奨と違う答えを示す言い方
DIFFERENT_RE = re.compile(r"ではなく|じゃなく|でなく|以外|違う|違い|変え|変更|別の|やめ|修正")
# 「食い違いはありません」「変更は不要」のように、違いがないと言っている言い方（先に取り除く）
NO_DIFF_RE = re.compile(r"(?:食い)?違(?:い|う点|うところ)(?:は|が|も)?(?:特に|何も)?(?:ありません|ございません|ない|なし)"
                        r"|(?:変更|修正)(?:点|の必要)?(?:は|も)?(?:特に)?(?:不要|ありません|ない|なし|必要ありません)")


def has_marker(option):
    return bool(MARKER_RE.search(option or ""))


def strip_marker(option):
    return MARKER_RE.sub("", option or "").strip()


def options_error(options):
    """登録時の検査。問題があれば理由の文字列。"""
    n = sum(1 for o in options if has_marker(o))
    if n > 1:
        return f"推奨（「（推奨）」の印）は1つだけにする（{n} 個ある）"
    if any(has_marker(o) and not strip_marker(o) for o in options):
        return "「（推奨）」の印だけで中身のない選択肢がある"
    return None


def recommended_option(q):
    """「（推奨）」の印がちょうど1つ付いた選択肢（印を除いた文字列）。なければ None。"""
    marked = [o for o in q.get("options") or [] if has_marker(o)]
    if len(marked) != 1 or not strip_marker(marked[0]):
        return None
    return strip_marker(marked[0])


def other_options(q):
    return [o for o in q.get("options") or [] if not has_marker(o)]


def fingerprint_items(questions):
    """仕様の指紋に入れる、判断シートで決める（決めた）質問。承認の前後で同じ値になる。"""
    items = []
    for q in questions:
        if q.get("answered_by") == "spec_approval":
            items.append([q["id"], q.get("text"), q.get("answer")])
        elif q.get("status") != "answered" and recommended_option(q):
            items.append([q["id"], q.get("text"), recommended_option(q)])
    return items


def _norm(t):
    return re.sub(r"\s+", "", t or "")


def _named(said, qid):
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(qid)}(?![0-9])", said) is not None


def reply_conflicts(said, open_questions):
    """承認の返答が判断シートの推奨と食い違う（かもしれない）理由。空なら推奨どおりに決めてよい。"""
    said = said or ""
    flat = _norm(said)
    out = []
    for q in open_questions:
        rec = recommended_option(q)
        chosen = [o for o in other_options(q) if len(_norm(o)) >= 2 and _norm(o) in flat]
        if chosen:
            out.append(f"{q['id']}: 推奨以外の選択肢（{' / '.join(chosen)}）が返答にある")
            continue
        if not _named(said, q["id"]):
            continue
        if DIFFERENT_RE.search(NO_DIFF_RE.sub("", said)):
            out.append(f"{q['id']}: 推奨と違う答えを示す言葉がある")
        elif "推奨" not in said and _norm(rec) not in flat:
            out.append(f"{q['id']}: 質問に触れているが、推奨どおりとは書かれていない")
    return out
