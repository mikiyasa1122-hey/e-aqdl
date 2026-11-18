"""メモの保存・取得・更新・削除・引き継ぎ・監査ログ（SQLite）。

認可は渡された利用者 dict（id・org_id・role）と DB 上の行（org_id・owner_id・private）で判定する。
SQL は全てパラメータ化し、書き込みは `_tx`（BEGIN IMMEDIATE）の中で原子的に行う。
"""
import contextlib
import logging
import time

from notes import db
from notes.errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

# 利用者に見えるメモの条件（同じ組織の公開メモ、または自分の private メモ）。
# 引数は (org_id, user_id)。全ての読み取り・更新・削除に付ける。
VISIBLE_SQL = "org_id = ? AND (private = 0 OR owner_id = ?)"

# 更新の SQL。version 条件が競合検出の最終判定（rowcount）になる。
# 引数は (title, body, updated_at, id, version, org_id, user_id)。
UPDATE_SQL = ("UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
              "WHERE id = ? AND version = ? AND {visible}")

_ORDER_BY = {
    "updated": "updated_at DESC, id DESC",
    "title": "title ASC, id ASC",
}

MAX_TITLE = 200
MAX_BODY = 100000
MAX_TAGS = 20
MAX_TAG = 32


class _Hooks:
    """テストが並行操作や途中失敗を再現するための差し込み口。既定は何もしない。"""

    def before_write(self, step):
        return None


hooks = _Hooks()


@contextlib.contextmanager
def _tx(conn):
    """書き込みトランザクション。失敗時は必ず ROLLBACK し、例外はそのまま伝える。"""
    # BEGIN IMMEDIATE が失敗（ロック待ち超過）したときは何も開いていないので変換しない
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --- 入力の検証 ---

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_title(title):
    if not isinstance(title, str):
        raise ValidationError("title must be a string")
    title = title.strip()
    if not 1 <= len(title) <= MAX_TITLE:
        raise ValidationError("title length must be 1..%d" % MAX_TITLE)
    return title


def _clean_body(body):
    if not isinstance(body, str):
        raise ValidationError("body must be a string")
    if len(body) > MAX_BODY:
        raise ValidationError("body is too long")
    return body


def _clean_tags(tags):
    """タグを検証し、重複を除いた名前順の list を返す。"""
    if isinstance(tags, (str, bytes, dict)) or tags is None:
        raise ValidationError("tags must be a collection of strings")
    try:
        items = list(tags)
    except TypeError:
        raise ValidationError("tags must be a collection of strings") from None
    cleaned = set()
    for tag in items:
        if not isinstance(tag, str):
            raise ValidationError("tag must be a string")
        tag = tag.strip()
        if not 1 <= len(tag) <= MAX_TAG:
            raise ValidationError("tag length must be 1..%d" % MAX_TAG)
        cleaned.add(tag)
    if len(cleaned) > MAX_TAGS:
        raise ValidationError("too many tags")
    return sorted(cleaned)


def _check_note_id(note_id):
    if not _is_int(note_id):
        raise ValidationError("note_id must be an integer")


# --- 内部の部品 ---

def _vis_args(user):
    return (user["org_id"], user["id"])


def _row_visible(conn, user, note_id):
    """利用者に見えるメモの行を返す。見えない・存在しない場合は NotFound（区別しない）。"""
    row = conn.execute("SELECT * FROM notes WHERE id = ? AND " + VISIBLE_SQL,
                       (note_id,) + _vis_args(user)).fetchone()
    if row is None:
        raise NotFound("note %s not found" % (note_id,))
    return row


def _check_can_write(user, row):
    if row["owner_id"] != user["id"] and user.get("role") != "admin":
        raise Forbidden("not allowed")


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _mask_key(api_key):
    api_key = str(api_key)
    if len(api_key) <= 4:
        return "*" * len(api_key)
    return "*" * (len(api_key) - 4) + api_key[-4:]


def _insert_tags(conn, note_id, tags):
    conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                     [(note_id, t) for t in tags])


def _tags_of(conn, note_id):
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]


# --- 公開 API ---

def create_note(conn, user, title, body, tags=(), private=False):
    title = _clean_title(title)
    body = _clean_body(body)
    tags = _clean_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, int(bool(private)), int(time.time())))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        _insert_tags(conn, note_id, tags)
        _audit(conn, user, "create", "note=%d" % note_id)
    logger.info("note created id=%s owner=%s", note_id, user["id"])
    return note_id


def get_note(conn, user, note_id):
    _row_visible(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        # 加算は SQL の中で行う（読み出して +1 する方式は同時 get で失われる）
        cur = conn.execute("UPDATE notes SET views = views + 1 WHERE id = ? AND " + VISIBLE_SQL,
                           (note_id,) + _vis_args(user))
        if cur.rowcount != 1:
            raise NotFound("note %s not found" % (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if not _is_int(page) or page < 1:
        raise ValidationError("page must be an integer >= 1")
    if not _is_int(per_page) or not 1 <= per_page <= 100:
        raise ValidationError("per_page must be 1..100")
    if not isinstance(sort, str) or sort not in _ORDER_BY:
        raise ValidationError("unknown sort")
    order = _ORDER_BY[sort]  # 固定の文字列から選ぶ（入力は連結しない）
    args = _vis_args(user)
    # 件数と一覧を同じ読み取りトランザクションで取る（書き込みロックは取らない）
    conn.execute("BEGIN")
    try:
        total = conn.execute("SELECT COUNT(*) FROM notes WHERE " + VISIBLE_SQL, args).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, updated_at FROM notes WHERE " + VISIBLE_SQL +
            " ORDER BY " + order + " LIMIT ? OFFSET ?",
            args + (per_page, (page - 1) * per_page)).fetchall()
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def _like_pattern(q):
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or q.strip() == "":
        raise ValidationError("q must be a non-empty string")
    tags = _clean_tags(tags)
    pattern = _like_pattern(q)
    sql = ("SELECT id, title FROM notes WHERE " + VISIBLE_SQL +
           " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    args = _vis_args(user) + (pattern, pattern)
    if tags:
        marks = ",".join("?" * len(tags))  # 動的なのは ? の個数だけ
        sql += (" AND id IN (SELECT note_id FROM note_tags WHERE tag IN (" + marks +
                ") GROUP BY note_id HAVING COUNT(DISTINCT tag) = ?)")
        args += tuple(tags) + (len(tags),)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args)]


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    _check_note_id(note_id)
    if not _is_int(version):
        raise ValidationError("version must be an integer")
    if title is not None:
        title = _clean_title(title)
    if body is not None:
        body = _clean_body(body)
    if tags is not None:
        tags = _clean_tags(tags)
    # 最初の確認（version はここでは比較しない。最終判定は UPDATE の rowcount）
    row = _row_visible(conn, user, note_id)
    _check_can_write(user, row)
    hooks.before_write("update")
    with _tx(conn):
        # 確認をトランザクション内でやり直す（途中で削除・所有者変更されうる）
        row = _row_visible(conn, user, note_id)
        _check_can_write(user, row)
        new_title = row["title"] if title is None else title
        new_body = row["body"] if body is None else body
        cur = conn.execute(UPDATE_SQL.format(visible=VISIBLE_SQL),
                           (new_title, new_body, int(time.time()), note_id, version) + _vis_args(user))
        if cur.rowcount != 1:
            raise Conflict("version conflict on note %s" % (note_id,))
        if tags is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, tags)
        _audit(conn, user, "update", "note=%d" % note_id)
        new_version = conn.execute("SELECT version FROM notes WHERE id = ?", (note_id,)).fetchone()[0]
    return new_version


def delete_note(conn, user, note_id):
    _check_note_id(note_id)
    row = _row_visible(conn, user, note_id)
    _check_can_write(user, row)
    with _tx(conn):
        row = _row_visible(conn, user, note_id)
        _check_can_write(user, row)
        # 外部キーのため、タグを先に消す
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        cur = conn.execute("DELETE FROM notes WHERE id = ? AND " + VISIBLE_SQL,
                           (note_id,) + _vis_args(user))
        if cur.rowcount != 1:
            raise NotFound("note %s not found" % (note_id,))
        _audit(conn, user, "delete", "note=%d" % note_id)


def _user_id(value):
    if isinstance(value, dict):
        value = value.get("id")
    if not _is_int(value):
        raise ValidationError("user id must be an integer")
    return value


def _require_admin(user):
    if user.get("role") != "admin":
        raise Forbidden("admin only")


def transfer_notes(conn, admin, from_user, to_user):
    _require_admin(admin)
    from_id = _user_id(from_user)
    to_id = _user_id(to_user)
    if from_id == to_id:
        raise ValidationError("from_user and to_user must differ")
    org_id = admin["org_id"]
    with _tx(conn):
        for uid in (from_id, to_id):
            u = db.get_user(conn, uid)
            if u is None or u["org_id"] != org_id:
                raise NotFound("user %s not found" % (uid,))
        # private も含めて移す（可視条件は使わず、組織条件だけを付ける）
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id", (from_id, org_id))]
        for note_id in ids:
            hooks.before_write("transfer:%d" % note_id)
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 "
                         "WHERE id = ? AND org_id = ? AND owner_id = ?",
                         (to_id, note_id, org_id, from_id))
        _audit(conn, admin, "transfer", "from=%d to=%d count=%d" % (from_id, to_id, len(ids)))
    return len(ids)


def export_audit(conn, admin):
    _require_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a "
        "LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["name"], "actor_key": _mask_key(r["api_key"]) if r["api_key"] is not None else None}
            for r in rows]
