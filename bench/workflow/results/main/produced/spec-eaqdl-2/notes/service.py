"""メモのサービス層。

SQL はすべて固定の文字列リテラルで、値は ? で渡す（文字列連結をしない）。
書き込みは _tx（BEGIN IMMEDIATE）に集約し、例外時は必ずロールバックする。
"""
import logging
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")
logger.addHandler(logging.NullHandler())  # ライブラリとして未設定時の標準エラー出力を避ける

_TAG_RE = re.compile(r"[a-z0-9-]{1,20}")
_MAX_TAGS = 5
_NOT_FOUND_MSG = "メモが見つかりません"


class Hooks:
    """テストが並行・途中失敗を再現するための差し込み口。既定は何もしない。"""

    def before_write(self, step: str) -> None:
        return None


hooks = Hooks()

User = dict[str, Any]


# ---------------------------------------------------------------- トランザクション
@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    """書き込みロックを先に取り、正常ならコミット、例外ならロールバックして再送出する。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- 入力検証
def _validate_title(title: Any) -> None:
    if not isinstance(title, str) or not (1 <= len(title) <= 100) or not title.strip():
        raise ValidationError("題名は1〜100文字（空白のみ不可）で指定してください")


def _validate_body(body: Any) -> None:
    if not isinstance(body, str) or len(body) > 10000:
        raise ValidationError("本文は10000文字以内の文字列で指定してください")


def _validate_tags(tags: Any) -> list[str]:
    """タグの列を検査し、重複を除いた初出順のリストを返す。"""
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple)):
        raise ValidationError("tags はタグの列で指定してください")
    if len(tags) > _MAX_TAGS:
        raise ValidationError("タグは5個までです")
    for t in tags:
        if not isinstance(t, str) or _TAG_RE.fullmatch(t) is None:
            raise ValidationError("タグは英小文字・数字・ハイフンの1〜20文字で指定してください")
    return list(dict.fromkeys(tags))


def _validate_int(value: Any, name: str, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (
        high is not None and value > high
    ):
        raise ValidationError(f"{name} の範囲が不正です")
    return value


# ---------------------------------------------------------------- 共通補助
def _now() -> int:
    return int(time.time())


def _visible(user: User, row: sqlite3.Row) -> bool:
    return bool(row["org_id"] == user["org_id"] and (not row["private"] or row["owner_id"] == user["id"]))


def _load_visible(conn: sqlite3.Connection, user: User, note_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None or not _visible(user, row):
        raise NotFound(_NOT_FOUND_MSG)
    return row  # type: ignore[no-any-return]


def _check_writable(user: User, row: sqlite3.Row) -> None:
    if row["owner_id"] == user["id"]:
        return
    if user.get("role") == "admin" and row["org_id"] == user["org_id"]:
        return
    raise Forbidden("このメモを操作する権限がありません")


def _audit(conn: sqlite3.Connection, org_id: int, actor_id: int, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
        (org_id, actor_id, action, detail),
    )


def _insert_tags(conn: sqlite3.Connection, note_id: int, tags: Sequence[str]) -> None:
    for t in tags:
        conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (note_id, t))


def _tags_of(conn: sqlite3.Connection, note_id: int) -> list[str]:
    rows = conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,)).fetchall()
    return [r["tag"] for r in rows]


def _like_pattern(q: str) -> str:
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + esc + "%"


# ---------------------------------------------------------------- 作成
def create_note(
    conn: sqlite3.Connection,
    user: User,
    title: str,
    body: str,
    tags: Sequence[str] = (),
    private: bool = False,
) -> int:
    _validate_title(title)
    _validate_body(body)
    uniq = _validate_tags(tags)
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()),
        )
        note_id = cur.lastrowid
        assert note_id is not None
        hooks.before_write("create:tags")
        _insert_tags(conn, note_id, uniq)
        _audit(conn, user["org_id"], user["id"], "create", f"note={note_id}")
    logger.info("note created id=%d owner=%d", note_id, user["id"])
    return note_id


# ---------------------------------------------------------------- 閲覧
def get_note(conn: sqlite3.Connection, user: User, note_id: int) -> dict[str, Any]:
    _load_visible(conn, user, note_id)
    hooks.before_write("view")
    with _tx(conn):
        # フック中の削除・移管・非公開化に備えて再確認する
        _load_visible(conn, user, note_id)
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


# ---------------------------------------------------------------- 一覧・検索
_LIST_COUNT_SQL = (
    "SELECT COUNT(*) FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?)"
)
_LIST_SQL = {
    "updated": (
        "SELECT id, title, updated_at FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
        "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    ),
    "title": (
        "SELECT id, title, updated_at FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
        "ORDER BY title ASC, id ASC LIMIT ? OFFSET ?"
    ),
}
_SQLITE_MAX_INT = 2**63 - 1


def list_notes(
    conn: sqlite3.Connection, user: User, page: int = 1, per_page: int = 20, sort: str = "updated"
) -> dict[str, Any]:
    _validate_int(page, "page", 1)
    _validate_int(per_page, "per_page", 1, 50)
    if not isinstance(sort, str) or sort not in _LIST_SQL:
        raise ValidationError("sort は updated か title を指定してください")
    total = conn.execute(_LIST_COUNT_SQL, (user["org_id"], user["id"])).fetchone()[0]
    offset = (page - 1) * per_page
    items: list[dict[str, Any]] = []
    if offset <= _SQLITE_MAX_INT:
        rows = conn.execute(_LIST_SQL[sort], (user["org_id"], user["id"], per_page, offset)).fetchall()
        items = [dict(r) for r in rows]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


_SEARCH_SQL = (
    "SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
    "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\') ORDER BY id"
)
_SEARCH_TAG_SQL = (
    "SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
    "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\') "
    "AND id IN (SELECT note_id FROM note_tags WHERE tag IN (?, ?, ?, ?, ?)) ORDER BY id"
)


def search_notes(
    conn: sqlite3.Connection, user: User, q: str, tags: Sequence[str] = ()
) -> list[dict[str, Any]]:
    if not isinstance(q, str) or not (1 <= len(q) <= 100) or not q.strip():
        raise ValidationError("検索語は1〜100文字（空白のみ不可）で指定してください")
    uniq = _validate_tags(tags)
    pat = _like_pattern(q)
    base: tuple[Any, ...] = (user["org_id"], user["id"], pat, pat)
    if uniq:
        padded: list[str | None] = [*uniq, *([None] * (_MAX_TAGS - len(uniq)))]
        rows = conn.execute(_SEARCH_TAG_SQL, base + tuple(padded)).fetchall()
    else:
        rows = conn.execute(_SEARCH_SQL, base).fetchall()
    return [{"id": r["id"], "title": r["title"]} for r in rows]


# ---------------------------------------------------------------- 更新・削除
def update_note(
    conn: sqlite3.Connection,
    user: User,
    note_id: int,
    version: int,
    title: str | None = None,
    body: str | None = None,
    tags: Sequence[str] | None = None,
) -> int:
    if title is not None:
        _validate_title(title)
    if body is not None:
        _validate_body(body)
    uniq = _validate_tags(tags) if tags is not None else None

    row = _load_visible(conn, user, note_id)
    _check_writable(user, row)
    if row["version"] != version:
        raise Conflict("メモが更新されています（version が古い）")
    hooks.before_write("update")
    with _tx(conn):
        row = _load_visible(conn, user, note_id)
        _check_writable(user, row)
        if row["version"] != version:
            raise Conflict("メモが更新されています（version が古い）")
        cur = conn.execute(
            "UPDATE notes SET title = COALESCE(?, title), body = COALESCE(?, body), "
            "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (title, body, _now(), note_id, version),
        )
        if cur.rowcount != 1:
            raise Conflict("メモが更新されています")
        if uniq is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, uniq)
        _audit(conn, user["org_id"], user["id"], "update", f"note={note_id}")
    return version + 1


def delete_note(conn: sqlite3.Connection, user: User, note_id: int) -> None:
    row = _load_visible(conn, user, note_id)
    _check_writable(user, row)
    with _tx(conn):
        row = _load_visible(conn, user, note_id)
        _check_writable(user, row)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user["org_id"], user["id"], "delete", f"note={note_id}")


# ---------------------------------------------------------------- 移管
def transfer_notes(conn: sqlite3.Connection, admin: User, from_user: User, to_user: User) -> int:
    if not (
        admin.get("role") == "admin"
        and admin["org_id"] == from_user["org_id"]
        and admin["org_id"] == to_user["org_id"]
    ):
        raise Forbidden("移管する権限がありません")
    if from_user["id"] == to_user["id"]:
        raise ValidationError("移管元と移管先が同じです")
    with _tx(conn):
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                (from_user["id"], admin["org_id"]),
            ).fetchall()
        ]
        for nid in ids:
            hooks.before_write(f"transfer:{nid}")
            conn.execute(
                "UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?",
                (to_user["id"], nid),
            )
        _audit(
            conn, admin["org_id"], admin["id"], "transfer",
            f"from={from_user['id']} to={to_user['id']} count={len(ids)}",
        )
    return len(ids)


# ---------------------------------------------------------------- 監査ログ
def _mask_key(key: str | None) -> str | None:
    if key is None:
        return None
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def export_audit(conn: sqlite3.Connection, admin: User) -> list[dict[str, Any]]:
    if admin.get("role") != "admin":
        raise Forbidden("監査ログを出力する権限がありません")
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a "
        "LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "action": r["action"],
            "detail": r["detail"],
            "actor": r["name"],
            "actor_key": _mask_key(r["api_key"]),
        }
        for r in rows
    ]
