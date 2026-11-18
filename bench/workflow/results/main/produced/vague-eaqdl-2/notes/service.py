"""メモの業務ロジック（組織分離・権限・版管理・引き継ぎ・監査）。

設計書 .aq-dlc の design.md に従う。SQL の値は常にバインド変数で渡し、
SQL 文字列に連結するのは固定の断片だけにする。
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import Iterator, Sequence
from typing import Any

from .errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

_TITLE_MAX = 200
_BODY_MAX = 10000
_TAG_MAX = 32
_TAGS_MAX_COUNT = 10
_QUERY_MAX = 200
_PER_PAGE_MAX = 100

# 可視性と書き込み可否の条件。org と private の条件はここ 1 か所に集約する
_VISIBLE = "org_id = :org AND (private = 0 OR owner_id = :uid)"
_WRITABLE = _VISIBLE + " AND (owner_id = :uid OR :is_admin = 1)"

_ORDER = {
    "updated": "updated_at DESC, id DESC",
    "title": "title ASC, id ASC",
}


class Hooks:
    """テストが並行操作や途中の失敗を再現するための差し込み口。"""

    def before_write(self, step: str) -> None:
        pass


hooks = Hooks()


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- 入力検証

def _check_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} は整数で指定してください")
    return value


def _check_utf8(value: str, name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{name} に使えない文字が含まれています") from None


def _check_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("title は文字列で指定してください")
    _check_utf8(value, "title")
    title = value.strip()
    if not 1 <= len(title) <= _TITLE_MAX:
        raise ValidationError(f"title は 1〜{_TITLE_MAX} 字で指定してください")
    return title


def _check_body(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("body は文字列で指定してください")
    if len(value) > _BODY_MAX:
        raise ValidationError(f"body は {_BODY_MAX} 字以下で指定してください")
    _check_utf8(value, "body")
    return value


def _check_tags(value: Any, name: str = "tags") -> list[str]:
    """タグの集まりを検証し、重複を除いた名前順のリストを返す。"""
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValidationError(f"{name} は list/tuple/set で指定してください")
    seen = set()
    for tag in value:
        if not isinstance(tag, str):
            raise ValidationError(f"{name} の要素は文字列で指定してください")
        if len(tag) > _TAG_MAX or not tag.strip():
            raise ValidationError(f"{name} の各要素は 1〜{_TAG_MAX} 字（空白のみ不可）で指定してください")
        _check_utf8(tag, name)
        seen.add(tag)
        if len(seen) > _TAGS_MAX_COUNT:
            raise ValidationError(f"{name} は {_TAGS_MAX_COUNT} 個までです")
    return sorted(seen)


def _check_private(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("private は真偽値で指定してください")
    return value


def _in_int64(value: int) -> bool:
    return _INT64_MIN <= value <= _INT64_MAX


# ---------------------------------------------------------------- トランザクション

def _convert_sqlite_error(exc: sqlite3.Error) -> Exception | None:
    """業務エラーに変換できる sqlite3 例外だけ変換する。それ以外は None。"""
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return Conflict("書き込みが競合しました。再試行してください")
    if isinstance(exc, sqlite3.IntegrityError):
        return ValidationError("利用者が不正です")
    return None


@contextlib.contextmanager
def _txn(conn: sqlite3.Connection, write: bool = True) -> Iterator[None]:
    """明示トランザクション。正常終了で COMMIT、例外で ROLLBACK する。"""
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    except sqlite3.Error as exc:
        converted = _convert_sqlite_error(exc)
        if converted is None:
            raise
        raise converted from None
    try:
        yield
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        if isinstance(exc, sqlite3.Error):
            converted = _convert_sqlite_error(exc)
            if converted is not None:
                raise converted from None
        raise


# ---------------------------------------------------------------- 共通の補助

def _params(user: dict[str, Any], **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "org": user["org_id"],
        "uid": user["id"],
        "is_admin": 1 if user["role"] == "admin" else 0,
    }
    params.update(extra)
    return params


def _is_admin(user: dict[str, Any]) -> bool:
    return user["role"] == "admin"


def _audit(conn: sqlite3.Connection, user: dict[str, Any], action: str, detail: str) -> None:
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _tags_of(conn: sqlite3.Connection, note_id: int) -> list[str]:
    rows = conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))
    return [r["tag"] for r in rows]


def _insert_tags(conn: sqlite3.Connection, note_id: int, tags: Sequence[str]) -> None:
    for tag in tags:
        conn.execute("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", (note_id, tag))


def _find_visible(conn: sqlite3.Connection, user: dict[str, Any], note_id: int) -> sqlite3.Row:
    """見えるメモの行（owner_id のみ）を返す。見えなければ NotFound。"""
    if not _in_int64(note_id):
        raise NotFound("メモが見つかりません")
    row = conn.execute("SELECT id, owner_id FROM notes WHERE id = :id AND " + _VISIBLE,
                       _params(user, id=note_id)).fetchone()
    if row is None:
        raise NotFound("メモが見つかりません")
    return row  # type: ignore[no-any-return]


def _find_writable(conn: sqlite3.Connection, user: dict[str, Any], note_id: int) -> None:
    """見えて、かつ書き込めることを確認する。NotFound / Forbidden。"""
    row = _find_visible(conn, user, note_id)
    if not (row["owner_id"] == user["id"] or _is_admin(user)):
        raise Forbidden("このメモを変更する権限がありません")


# ---------------------------------------------------------------- 公開関数

def create_note(conn: sqlite3.Connection, user: dict[str, Any], title: Any, body: Any,
                tags: Any = (), private: Any = False) -> int:
    title_v = _check_title(title)
    body_v = _check_body(body)
    tags_v = _check_tags(tags)
    private_v = _check_private(private)
    with _txn(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user["org_id"], user["id"], title_v, body_v, 1 if private_v else 0, _now()))
        note_id = cur.lastrowid
        assert note_id is not None
        hooks.before_write("create:tags")
        _insert_tags(conn, note_id, tags_v)
        _audit(conn, user, "create", f"note={note_id}")
    logger.info("note created id=%s owner=%s", note_id, user["id"])
    return note_id


def get_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: Any) -> dict[str, Any]:
    _check_int(note_id, "note_id")
    _find_visible(conn, user, note_id)
    hooks.before_write("view")
    with _txn(conn):
        cur = conn.execute("UPDATE notes SET views = views + 1 WHERE id = :id AND " + _VISIBLE,
                           _params(user, id=note_id))
        if cur.rowcount != 1:
            raise NotFound("メモが見つかりません")
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        result = dict(row)
        result["tags"] = _tags_of(conn, note_id)
    return result


def list_notes(conn: sqlite3.Connection, user: dict[str, Any], page: Any = 1,
               per_page: Any = 20, sort: Any = "updated") -> dict[str, Any]:
    _check_int(page, "page")
    _check_int(per_page, "per_page")
    if page < 1:
        raise ValidationError("page は 1 以上で指定してください")
    if not 1 <= per_page <= _PER_PAGE_MAX:
        raise ValidationError(f"per_page は 1〜{_PER_PAGE_MAX} で指定してください")
    if not isinstance(sort, str) or sort not in _ORDER:
        raise ValidationError("sort は updated か title で指定してください")
    items: list[dict[str, Any]] = []
    with _txn(conn, write=False):
        total = conn.execute("SELECT COUNT(*) FROM notes WHERE " + _VISIBLE,
                             _params(user)).fetchone()[0]
        offset = (page - 1) * per_page
        if offset < total:
            rows = conn.execute(
                "SELECT id, title, updated_at FROM notes WHERE " + _VISIBLE +
                " ORDER BY " + _ORDER[sort] + " LIMIT :limit OFFSET :offset",
                _params(user, limit=per_page, offset=offset))
            items = [dict(r) for r in rows]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(conn: sqlite3.Connection, user: dict[str, Any], q: Any,
                 tags: Any = ()) -> list[dict[str, Any]]:
    if not isinstance(q, str):
        raise ValidationError("q は文字列で指定してください")
    _check_utf8(q, "q")
    if not q.strip() or len(q) > _QUERY_MAX:
        raise ValidationError(f"q は空白のみ不可・{_QUERY_MAX} 字以下で指定してください")
    tags_v = _check_tags(tags)
    params = _params(user, p="%" + _like_escape(q) + "%")
    sql = ("SELECT id, title FROM notes WHERE " + _VISIBLE +
           " AND (title LIKE :p ESCAPE '\\' OR body LIKE :p ESCAPE '\\')")
    for i, tag in enumerate(tags_v):
        # 句の個数だけが可変。タグの値はバインド変数で渡す
        sql += (" AND EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = notes.id"
                f" AND t.tag = :t{i})")
        params[f"t{i}"] = tag
    sql += " ORDER BY id"
    with _txn(conn, write=False):
        return [dict(r) for r in conn.execute(sql, params)]


def update_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: Any, version: Any,
                title: Any = None, body: Any = None, tags: Any = None) -> int:
    _check_int(note_id, "note_id")
    _check_int(version, "version")
    title_v = None if title is None else _check_title(title)
    body_v = None if body is None else _check_body(body)
    tags_v = None if tags is None else _check_tags(tags)

    _find_writable(conn, user, note_id)
    if not _in_int64(version):
        raise Conflict("版番号が一致しません")
    hooks.before_write("update")
    with _txn(conn):
        cur = conn.execute(
            "UPDATE notes SET title = COALESCE(:title, title), body = COALESCE(:body, body), "
            "version = version + 1, updated_at = :now "
            "WHERE id = :id AND " + _WRITABLE + " AND version = :version",
            _params(user, id=note_id, title=title_v, body=body_v, now=_now(), version=version))
        if cur.rowcount != 1:
            # 原因を判別する（見えない・書けない・版の不一致）
            _find_writable(conn, user, note_id)
            raise Conflict("版番号が一致しません。最新の内容を取得し直してください")
        if tags_v is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, tags_v)
        _audit(conn, user, "update", f"note={note_id}")
        new_version = conn.execute("SELECT version FROM notes WHERE id = ?",
                                   (note_id,)).fetchone()[0]
    return new_version  # type: ignore[no-any-return]


def delete_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: Any) -> None:
    _check_int(note_id, "note_id")
    with _txn(conn):
        _find_writable(conn, user, note_id)
        # note_tags は CASCADE が無いので、必ず先にタグを消す
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        cur = conn.execute("DELETE FROM notes WHERE id = :id AND " + _WRITABLE,
                           _params(user, id=note_id))
        if cur.rowcount != 1:
            raise NotFound("メモが見つかりません")
        _audit(conn, user, "delete", f"note={note_id}")


def transfer_notes(conn: sqlite3.Connection, admin: dict[str, Any], from_user: Any,
                   to_user: Any) -> int:
    _check_int(from_user, "from_user")
    _check_int(to_user, "to_user")
    if not _is_admin(admin):
        raise Forbidden("管理者だけが実行できます")
    if from_user == to_user:
        raise ValidationError("引き継ぎ元と先が同じです")
    if not (_in_int64(from_user) and _in_int64(to_user)):
        raise NotFound("利用者が見つかりません")
    with _txn(conn):
        rows = conn.execute("SELECT id FROM users WHERE id IN (:f, :t) AND org_id = :org",
                            {"f": from_user, "t": to_user, "org": admin["org_id"]}).fetchall()
        if len(rows) != 2:
            raise NotFound("利用者が見つかりません")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = :f AND org_id = :org ORDER BY id",
            {"f": from_user, "org": admin["org_id"]})]
        for note_id in ids:
            hooks.before_write(f"transfer:{note_id}")
            conn.execute(
                "UPDATE notes SET owner_id = :t, version = version + 1, updated_at = :now "
                "WHERE id = :id AND owner_id = :f AND org_id = :org",
                {"t": to_user, "f": from_user, "org": admin["org_id"], "id": note_id,
                 "now": _now()})
        _audit(conn, admin, "transfer",
               f"from={from_user} to={to_user} count={len(ids)}")
    return len(ids)


def _mask(key: str | None) -> str:
    """api_key の伏せ字。末尾 4 文字だけ残す（4 文字以下は全部伏せる）。"""
    if not key:
        return ""
    if len(key) > 4:
        return "*" * (len(key) - 4) + key[-4:]
    return "*" * len(key)


def export_audit(conn: sqlite3.Connection, admin: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_admin(admin):
        raise Forbidden("管理者だけが実行できます")
    with _txn(conn, write=False):
        rows = conn.execute(
            "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a "
            "LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = :org ORDER BY a.id",
            {"org": admin["org_id"]}).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"],
             "actor": r["name"], "actor_key": _mask(r["api_key"])} for r in rows]
