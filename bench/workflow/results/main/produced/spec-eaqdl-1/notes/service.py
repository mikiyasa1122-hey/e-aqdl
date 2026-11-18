"""メモの業務ロジック（作成・閲覧・一覧・検索・更新・削除・移管・監査ログ）。

接続は isolation_level=None（自動コミット）なので、トランザクションは _tx で明示する。
認可は引数の利用者 dict の id・org_id・role と DB の行だけで判定する。
"""
import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from notes.errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")
logger.addHandler(logging.NullHandler())  # ライブラリとして、呼び出し側が設定するまで出力しない

_TAG_RE = re.compile(r"[a-z0-9-]{1,20}")
_MAX_TITLE = 100
_MAX_BODY = 10000
_MAX_TAGS = 5
_MAX_QUERY = 100

# 並び順は許可リストから固定の断片を選ぶだけ（入力を SQL に埋め込まない）
_SORTS = {
    "updated": "updated_at DESC, id DESC",
    "title": "title ASC, id ASC",
}

# 可視条件の SQL 断片（一覧・検索で共有する 1 か所の定義）。値は ? で渡す: org_id, user_id
_VISIBLE_SQL = "org_id = ? AND (private = 0 OR owner_id = ?)"


class _Hooks:
    """テストが並行・途中失敗を再現するための差し込み口。既定は何もしない。"""

    def before_write(self, step: str) -> None:
        return None


hooks = _Hooks()


@contextmanager
def _tx(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Iterator[None]:
    """トランザクション。例外ならロールバックして再送出する（握りつぶさない）。"""
    conn.execute("BEGIN IMMEDIATE" if mode == "IMMEDIATE" else "BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---- 入力検証 ----

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_title(title: Any) -> None:
    if not isinstance(title, str) or not (1 <= len(title) <= _MAX_TITLE) or not title.strip():
        raise ValidationError("invalid title")


def _validate_body(body: Any) -> None:
    if not isinstance(body, str) or len(body) > _MAX_BODY:
        raise ValidationError("invalid body")


def _validate_tags(tags: Any) -> list[str]:
    if isinstance(tags, (str, bytes)):
        raise ValidationError("invalid tags")
    try:
        items = list(tags)
    except TypeError:
        raise ValidationError("invalid tags") from None
    if len(items) > _MAX_TAGS:
        raise ValidationError("too many tags")
    for tag in items:
        if not isinstance(tag, str) or _TAG_RE.fullmatch(tag) is None:
            raise ValidationError("invalid tag")
    return items


def _validate_id(value: Any, what: str) -> int:
    if not _is_int(value):
        raise ValidationError(f"invalid {what}")
    return int(value)


# ---- 共通部品 ----

def _visible_note(conn: sqlite3.Connection, user: dict, note_id: Any) -> sqlite3.Row:
    """見られるメモの行を返す。見えないものは理由を区別せず NotFound。"""
    row = None
    if _is_int(note_id):
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None or row["org_id"] != user["org_id"]:
        raise NotFound("note not found")
    if row["private"] and row["owner_id"] != user["id"]:
        raise NotFound("note not found")
    return row


def _require_can_modify(user: dict, row: sqlite3.Row) -> None:
    """作成者、または同組織の管理者だけ（非公開は _visible_note が先に除く）。"""
    if row["owner_id"] == user["id"]:
        return
    if user["role"] == "admin" and row["org_id"] == user["org_id"]:
        return
    raise Forbidden("forbidden")


def _audit(conn: sqlite3.Connection, org_id: int, actor_id: int, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
        (org_id, actor_id, action, detail),
    )


def _mask_key(key: str) -> str:
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def _replace_tags(conn: sqlite3.Connection, note_id: int, tags: list[str]) -> None:
    for tag in sorted(set(tags)):
        conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (note_id, tag))


# ---- 公開関数 ----

def create_note(conn, user, title, body, tags=(), private=False) -> int:
    _validate_title(title)
    _validate_body(body)
    tag_list = _validate_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, int(time.time())),
        )
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        _replace_tags(conn, note_id, tag_list)
        _audit(conn, user["org_id"], user["id"], "create", f"note={note_id}")
    # 失敗したのに「作成した」と記録しないよう COMMIT の後に出す
    logger.info("note created id=%s owner=%s", note_id, user["id"])
    return note_id


def _read_note(conn: sqlite3.Connection, note_id: int) -> dict:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    note = dict(row)
    note["tags"] = [
        r["tag"]
        for r in conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))
    ]
    return note


def get_note(conn, user, note_id) -> dict:
    _visible_note(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        # 割り込みで削除・移管・非公開化されていないか、トランザクション内で確かめ直す
        _visible_note(conn, user, note_id)
        # 読んで足して書く形にせず、SQL の 1 文で加算する（数え漏れ防止）
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        return _read_note(conn, note_id)


def _check_page(page: Any, per_page: Any, sort: Any) -> str:
    if not _is_int(page) or page < 1:
        raise ValidationError("invalid page")
    if not _is_int(per_page) or not (1 <= per_page <= 50):
        raise ValidationError("invalid per_page")
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ValidationError("invalid sort")
    return _SORTS[sort]


def list_notes(conn, user, page=1, per_page=20, sort="updated") -> dict:
    order = _check_page(page, per_page, sort)
    params = (user["org_id"], user["id"])
    # 件数と一覧を 1 つの読み取りトランザクションで取り、ずれないようにする
    with _tx(conn, "DEFERRED"):
        total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {_VISIBLE_SQL}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, title, updated_at FROM notes WHERE {_VISIBLE_SQL} ORDER BY {order} LIMIT ? OFFSET ?",
            params + (per_page, (page - 1) * per_page),
        ).fetchall()
    return {
        "items": [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"]} for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(conn, user, q, tags=()) -> list:
    if not isinstance(q, str) or not (1 <= len(q) <= _MAX_QUERY) or not q.strip():
        raise ValidationError("invalid query")
    tag_list = _validate_tags(tags)
    pattern = f"%{_escape_like(q)}%"
    sql = (
        f"SELECT id, title FROM notes WHERE {_VISIBLE_SQL}"
        " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
    )
    params: list[Any] = [user["org_id"], user["id"], pattern, pattern]
    if tag_list:
        marks = ",".join("?" for _ in tag_list)
        sql += f" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = notes.id AND t.tag IN ({marks}))"
        params.extend(tag_list)
    sql += " ORDER BY id"
    return [{"id": r["id"], "title": r["title"]} for r in conn.execute(sql, params).fetchall()]


def update_note(conn, user, note_id, version, title=None, body=None, tags=None) -> int:
    if not _is_int(version):
        raise ValidationError("invalid version")
    if title is not None:
        _validate_title(title)
    if body is not None:
        _validate_body(body)
    tag_list = _validate_tags(tags) if tags is not None else None

    row = _visible_note(conn, user, note_id)
    _require_can_modify(user, row)
    if row["version"] != version:
        raise Conflict("version conflict")
    hooks.before_write("update")

    with _tx(conn):
        # 確認から書き込みまでの間の割り込みを拾うため、トランザクション内で確かめ直す
        row = _visible_note(conn, user, note_id)
        _require_can_modify(user, row)
        if row["version"] != version:
            raise Conflict("version conflict")
        new_title = row["title"] if title is None else title
        new_body = row["body"] if body is None else body
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ?"
            " WHERE id = ? AND version = ?",
            (new_title, new_body, int(time.time()), note_id, version),
        )
        if cur.rowcount != 1:
            raise Conflict("version conflict")
        if tag_list is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _replace_tags(conn, note_id, tag_list)
        _audit(conn, user["org_id"], user["id"], "update", f"note={note_id}")
    return version + 1


def delete_note(conn, user, note_id) -> None:
    row = _visible_note(conn, user, note_id)
    _require_can_modify(user, row)
    with _tx(conn):
        row = _visible_note(conn, user, note_id)
        _require_can_modify(user, row)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if cur.rowcount != 1:
            raise NotFound("note not found")
        _audit(conn, user["org_id"], user["id"], "delete", f"note={note_id}")


def _user_org(conn: sqlite3.Connection, user: Any) -> tuple[int, int]:
    """利用者（dict または id）の (id, org_id) を DB から引き直す。"""
    uid = user["id"] if isinstance(user, dict) else user
    if not _is_int(uid):
        raise ValidationError("invalid user")
    row = conn.execute("SELECT id, org_id FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        raise NotFound("user not found")
    return row["id"], row["org_id"]


def transfer_notes(conn, admin, from_user, to_user) -> int:
    if admin["role"] != "admin":
        raise Forbidden("forbidden")
    from_id, from_org = _user_org(conn, from_user)
    to_id, to_org = _user_org(conn, to_user)
    org = admin["org_id"]
    if from_org != org or to_org != org:
        raise Forbidden("forbidden")
    with _tx(conn):
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id", (from_id, org)
            ).fetchall()
        ]
        for nid in ids:
            hooks.before_write(f"transfer:{nid}")
            conn.execute(
                "UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?", (to_id, nid)
            )
        _audit(conn, org, admin["id"], "transfer", f"from={from_id} to={to_id} count={len(ids)}")
    return len(ids)


def export_audit(conn, admin) -> list:
    if admin["role"] != "admin":
        raise Forbidden("forbidden")
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a"
        " LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],),
    ).fetchall()
    result = []
    for r in rows:
        key = r["api_key"]
        result.append({
            "id": r["id"],
            "action": r["action"],
            "detail": r["detail"],
            "actor": r["name"],
            # 生の鍵は取得直後に伏せ、持ち回らない
            "actor_key": _mask_key(key) if key is not None else None,
        })
    return result
