"""メモのサービス層。トランザクションは自前の BEGIN IMMEDIATE で管理する。"""
import logging
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from notes.errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

_TAG_RE = re.compile(r"[a-z0-9-]{1,20}")
_MAX_TAGS = 5
_SORTS = {
    "updated": "updated_at DESC, id DESC",
    "title": "title ASC, id ASC",
}


class _Hooks:
    """テストが並行の操作・途中の失敗を再現するための差し込み口。"""

    def before_write(self, step: str) -> None:
        pass


hooks = _Hooks()


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE ... COMMIT。どの例外でも ROLLBACK し、元の例外を再送出する。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # ROLLBACK の失敗で元の例外を隠さない
        raise


def _now() -> int:
    return int(time.time())


def _validate_title(title: Any) -> None:
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValidationError("題名は空白以外を含む1〜100文字の文字列にしてください")


def _validate_body(body: Any) -> None:
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("本文は10000文字までの文字列にしてください")


def _validate_tags(tags: Any) -> list[str]:
    """タグの列を検査し、重複を除いた（順序を保つ）リストを返す。"""
    if not isinstance(tags, (list, tuple)):
        raise ValidationError("タグは文字列の列で指定してください")
    if len(tags) > _MAX_TAGS:
        raise ValidationError("タグは5個までです")
    for t in tags:
        if not isinstance(t, str) or _TAG_RE.fullmatch(t) is None:
            raise ValidationError("タグは英小文字・数字・ハイフンの1〜20文字にしてください")
    return list(dict.fromkeys(tags))


def _validate_query(q: Any) -> None:
    if not isinstance(q, str) or not q.strip() or len(q) > 100:
        raise ValidationError("検索語は空白以外を含む1〜100文字にしてください")


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fetch_visible(conn: sqlite3.Connection, user: dict[str, Any], note_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM notes WHERE id = ? AND org_id = ? AND (private = 0 OR owner_id = ?)",
        (note_id, user["org_id"], user["id"]),
    ).fetchone()
    if row is None:
        raise NotFound("メモが見つかりません")
    return row  # type: ignore[no-any-return]


def _can_modify(user: dict[str, Any], row: sqlite3.Row) -> None:
    if user["id"] != row["owner_id"] and user["role"] != "admin":
        raise Forbidden("このメモを操作する権限がありません")


def _audit(conn: sqlite3.Connection, org_id: int, actor_id: int, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
        (org_id, actor_id, action, detail),
    )


def _mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def _insert_tags(conn: sqlite3.Connection, note_id: int, tags: Sequence[str]) -> None:
    for t in tags:
        conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (note_id, t))


def create_note(conn: sqlite3.Connection, user: dict[str, Any], title: str, body: str,
                tags: Sequence[str] = (), private: bool = False) -> int:
    _validate_title(title)
    _validate_body(body)
    uniq = _validate_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()),
        )
        note_id = int(cur.lastrowid or 0)
        hooks.before_write("create:tags")
        _insert_tags(conn, note_id, uniq)
        _audit(conn, user["org_id"], user["id"], "create", f"note={note_id}")
    logger.info("メモを作成しました id=%s owner=%s", note_id, user["id"])
    return note_id


def get_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: int) -> dict[str, Any]:
    _fetch_visible(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        # 閲覧数は SQL 側で原子的に加算し、可視性も同じ UPDATE で再確認する
        cur = conn.execute(
            "UPDATE notes SET views = views + 1 "
            "WHERE id = ? AND org_id = ? AND (private = 0 OR owner_id = ?)",
            (note_id, user["org_id"], user["id"]),
        )
        if cur.rowcount == 0:
            raise NotFound("メモが見つかりません")
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        tags = [r["tag"] for r in conn.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
    result = dict(row)
    result["tags"] = tags
    return result


def list_notes(conn: sqlite3.Connection, user: dict[str, Any], page: int = 1,
               per_page: int = 20, sort: str = "updated") -> dict[str, Any]:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValidationError("page は1以上の整数にしてください")
    if not isinstance(per_page, int) or isinstance(per_page, bool) or not 1 <= per_page <= 50:
        raise ValidationError("per_page は1〜50の整数にしてください")
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ValidationError("sort は updated か title にしてください")
    order = _SORTS[sort]  # 固定の辞書から選ぶ（利用者の文字列は SQL に入れない）
    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
    params = (user["org_id"], user["id"])
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, title, updated_at FROM notes WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + (per_page, (page - 1) * per_page),
    ).fetchall()
    return {
        "items": [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"]} for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def search_notes(conn: sqlite3.Connection, user: dict[str, Any], q: str,
                 tags: Sequence[str] = ()) -> list[dict[str, Any]]:
    _validate_query(q)
    tag_list = _validate_tags(tags)
    pattern = "%" + _escape_like(q) + "%"
    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    params: list[Any] = [user["org_id"], user["id"], pattern, pattern]
    if tag_list:
        marks = ",".join("?" for _ in tag_list)  # 組み立てるのは ? の個数だけ
        sql += (" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = notes.id "
                f"AND t.tag IN ({marks}))")
        params.extend(tag_list)
    sql += " ORDER BY id"
    return [{"id": r["id"], "title": r["title"]} for r in conn.execute(sql, params)]


def update_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: int, version: int,
                title: str | None = None, body: str | None = None,
                tags: Sequence[str] | None = None) -> int:
    if title is not None:
        _validate_title(title)
    if body is not None:
        _validate_body(body)
    uniq = _validate_tags(tags) if tags is not None else None
    row = _fetch_visible(conn, user, note_id)
    _can_modify(user, row)
    if row["version"] != version:
        raise Conflict("メモが他の更新で変更されています")
    hooks.before_write("update")
    with _tx(conn):
        cur = conn.execute(
            "UPDATE notes SET title = COALESCE(?, title), body = COALESCE(?, body), "
            "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (title, body, _now(), note_id, version),
        )
        if cur.rowcount == 0:
            exists = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
            if exists is None:
                raise NotFound("メモが見つかりません")
            raise Conflict("メモが他の更新で変更されています")
        if uniq is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, uniq)
        _audit(conn, user["org_id"], user["id"], "update", f"note={note_id}")
    return version + 1


def delete_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: int) -> None:
    row = _fetch_visible(conn, user, note_id)
    _can_modify(user, row)
    with _tx(conn):
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if cur.rowcount == 0:
            raise NotFound("メモが見つかりません")
        _audit(conn, user["org_id"], user["id"], "delete", f"note={note_id}")


def transfer_notes(conn: sqlite3.Connection, admin: dict[str, Any], from_user: dict[str, Any],
                   to_user: dict[str, Any]) -> int:
    if admin["role"] != "admin":
        raise Forbidden("管理者だけが実行できます")
    org = admin["org_id"]
    if from_user["org_id"] != org or to_user["org_id"] != org:
        raise Forbidden("同じ組織の利用者の間だけ移管できます")
    with _tx(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (from_user["id"], org))]
        for nid in ids:
            hooks.before_write(f"transfer:{nid}")
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?",
                         (to_user["id"], nid))
        _audit(conn, org, admin["id"], "transfer",
               f"from={from_user['id']} to={to_user['id']} count={len(ids)}")
    return len(ids)


def export_audit(conn: sqlite3.Connection, admin: dict[str, Any]) -> list[dict[str, Any]]:
    if admin["role"] != "admin":
        raise Forbidden("管理者だけが実行できます")
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a "
        "LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],),
    ).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["name"] or "", "actor_key": _mask_key(r["api_key"])} for r in rows]
