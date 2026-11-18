"""メモの業務ロジック（検証 → 可視性/権限 → トランザクション補助 → 公開関数）。

例外メッセージとログには、題名・本文・タグ・鍵などの入力値を入れない（R9）。
"""
import contextlib
import logging
import re
import time

from .errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

_TAG_RE = re.compile(r"[a-z0-9-]{1,20}", re.ASCII)
_NOT_FOUND_MSG = "メモが見つかりません"


class _Hooks:
    """テストが並行操作や途中失敗を再現するための差し込み口。既定は何もしない。"""

    def before_write(self, step):
        pass


hooks = _Hooks()


def _now():
    return int(time.time())


# ---- 検証 ----
def _validate_title(title):
    if not isinstance(title, str) or not (1 <= len(title) <= 100) or not title.strip():
        raise ValidationError("題名は1〜100文字で、空白だけは不可です")
    return title


def _validate_body(body):
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("本文は10000文字までの文字列です")
    return body


def _validate_tags(tags):
    # 文字列そのものは不可。列（list/tuple）だけを受ける
    if not isinstance(tags, (list, tuple)) or len(tags) > 5:
        raise ValidationError("タグは5個までの列です")
    for t in tags:
        if not isinstance(t, str) or _TAG_RE.fullmatch(t) is None:
            raise ValidationError("タグは英小文字・数字・ハイフンの1〜20文字です")
    return sorted(set(tags))


def _require_admin(user):
    if user.get("role") != "admin":
        raise Forbidden("管理者のみ実行できます")


# ---- トランザクション ----
@contextlib.contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _audit(conn, org_id, actor_id, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (org_id, actor_id, action, detail))


# ---- 可視性・権限 ----
def _fetch_visible(conn, user, note_id):
    row = conn.execute(
        "SELECT * FROM notes WHERE id = ? AND org_id = ? AND (private = 0 OR owner_id = ?)",
        (note_id, user["org_id"], user["id"])).fetchone()
    if row is None:
        # 他組織・他人の非公開・存在しないは同一の型とメッセージ（K13）
        raise NotFound(_NOT_FOUND_MSG)
    return row


def _require_writer(user, row):
    if row["owner_id"] != user["id"] and user.get("role") != "admin":
        raise Forbidden("このメモを変更する権限がありません")


def _tags_of(conn, note_id):
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]


# ---- 公開関数 ----
def create_note(conn, user, title, body, tags=(), private=False):
    _validate_title(title)
    _validate_body(body)
    tag_list = _validate_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        for t in tag_list:
            conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (note_id, t))
        _audit(conn, user["org_id"], user["id"], "create", f"note={note_id}")
    logger.info("メモを作成 note_id=%s user_id=%s", note_id, user["id"])
    return note_id


def get_note(conn, user, note_id):
    _fetch_visible(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        cur = conn.execute(
            "UPDATE notes SET views = views + 1 "
            "WHERE id = ? AND org_id = ? AND (private = 0 OR owner_id = ?)",
            (note_id, user["org_id"], user["id"]))
        if cur.rowcount == 0:
            raise NotFound(_NOT_FOUND_MSG)
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValidationError("page は1以上の整数です")
    if not isinstance(per_page, int) or isinstance(per_page, bool) or not (1 <= per_page <= 50):
        raise ValidationError("per_page は1〜50の整数です")
    orders = {"updated": "updated_at DESC, id DESC", "title": "title ASC, id ASC"}
    if not isinstance(sort, str) or sort not in orders:
        raise ValidationError("sort は updated か title です")
    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
    params = (user["org_id"], user["id"])
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, title, updated_at FROM notes WHERE {where} ORDER BY {orders[sort]} LIMIT ? OFFSET ?",
        params + (per_page, (page - 1) * per_page)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def _escape_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or not (1 <= len(q) <= 100) or not q.strip():
        raise ValidationError("検索語は1〜100文字で、空白だけは不可です")
    tag_list = _validate_tags(tags)
    like = "%" + _escape_like(q) + "%"
    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    params = [user["org_id"], user["id"], like, like]
    if tag_list:
        marks = ",".join("?" * len(tag_list))  # 個数だけを組み立てる（値は埋め込まない）
        sql += f" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = notes.id AND t.tag IN ({marks}))"
        params += tag_list
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    if title is not None:
        _validate_title(title)
    if body is not None:
        _validate_body(body)
    tag_list = _validate_tags(tags) if tags is not None else None
    row = _fetch_visible(conn, user, note_id)
    _require_writer(user, row)
    if row["version"] != version:
        raise Conflict("他の更新と競合しました")
    hooks.before_write("update")
    with _tx(conn):
        cur_row = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND org_id = ? AND (private = 0 OR owner_id = ?)",
            (note_id, user["org_id"], user["id"])).fetchone()
        if cur_row is None:
            raise NotFound(_NOT_FOUND_MSG)
        _require_writer(user, cur_row)
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (title if title is not None else cur_row["title"],
             body if body is not None else cur_row["body"],
             _now(), note_id, version))
        if cur.rowcount == 0:
            raise Conflict("他の更新と競合しました")
        if tag_list is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            for t in tag_list:
                conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (note_id, t))
        _audit(conn, user["org_id"], user["id"], "update", f"note={note_id}")
    return version + 1


def delete_note(conn, user, note_id):
    row = _fetch_visible(conn, user, note_id)
    _require_writer(user, row)
    with _tx(conn):
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        cur = conn.execute("DELETE FROM notes WHERE id = ? AND org_id = ?",
                           (note_id, user["org_id"]))
        if cur.rowcount == 0:
            raise NotFound(_NOT_FOUND_MSG)
        _audit(conn, user["org_id"], user["id"], "delete", f"note={note_id}")


def transfer_notes(conn, admin, from_user, to_user):
    _require_admin(admin)

    def _lookup(u):
        uid = u["id"] if isinstance(u, dict) else u
        r = conn.execute("SELECT id, org_id FROM users WHERE id = ?", (uid,)).fetchone()
        if r is None:
            raise NotFound("利用者が見つかりません")
        return r

    src, dst = _lookup(from_user), _lookup(to_user)
    org = admin["org_id"]
    if src["org_id"] != org or dst["org_id"] != org:
        raise Forbidden("組織外への移管はできません")
    with _tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id", (src["id"], org))]
        now = _now()
        for nid in ids:
            hooks.before_write(f"transfer:{nid}")
            conn.execute(
                "UPDATE notes SET owner_id = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND org_id = ?", (dst["id"], now, nid, org))
        _audit(conn, org, admin["id"], "transfer",
               f"from={src['id']} to={dst['id']} count={len(ids)}")
    return len(ids)


def _mask_key(key):
    key = key or ""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def export_audit(conn, admin):
    _require_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a "
        "LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["name"], "actor_key": _mask_key(r["api_key"])} for r in rows]
