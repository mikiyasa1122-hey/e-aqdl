"""メモのサービス層。組織分離・非公開・楽観的排他・引き継ぎ・監査を担う。

設計の要点（docs/contract.md と設計書に従う）:
- 可視条件は `_visible_clause` に一本化し、全操作がこれを通る。
- 書き込みは `_tx`（BEGIN IMMEDIATE）で囲み、例外時は ROLLBACK する。
- 例外メッセージは固定の日本語文で、入力値を埋め込まない。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from notes import db
from notes.errors import Conflict, Forbidden, NotFound, ValidationError

logger = logging.getLogger("notes")

# 長さの上限（仕様 A3）
MAX_TITLE = 200
MAX_BODY = 10000
MAX_TAGS = 20
MAX_TAG_LEN = 50
MAX_QUERY = 10000

_INT64_MAX = 2**63 - 1

_NOT_FOUND_MSG = "メモが見つかりません"


class Hooks:
    """テストが並行・途中失敗を再現するための差し込み口。既定は何もしない。"""

    def before_write(self, step: str) -> None:
        return None


hooks = Hooks()


def _now() -> int:
    """updated_at 用の現在時刻（秒）。テストは差し替えられる。"""
    return int(time.time())


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    """書き込みトランザクション。例外は ROLLBACK して再送出する。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- 入力検証

def _check_text(value: Any, label: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label}は文字列で指定してください")
    if len(value) > max_len:
        raise ValidationError(f"{label}が長すぎます")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{label}に不正な文字が含まれています") from None
    return value


def _validate_title(title: Any) -> str:
    _check_text(title, "題名", MAX_TITLE)
    if not title.strip():
        raise ValidationError("題名が空です")
    return str(title)


def _validate_body(body: Any) -> str:
    return _check_text(body, "本文", MAX_BODY)


def _validate_tags(tags: Any) -> list[str]:
    """タグ集合を検証し、重複を除いた名前順のリストを返す。"""
    if not isinstance(tags, (list, tuple, set, frozenset)):
        raise ValidationError("タグはリストで指定してください")
    seen: set[str] = set()
    for tag in tags:
        _check_text(tag, "タグ", MAX_TAG_LEN)
        if not tag.strip():
            raise ValidationError("タグが空です")
        seen.add(tag)
    if len(seen) > MAX_TAGS:
        raise ValidationError("タグが多すぎます")
    return sorted(seen)


def _validate_int(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label}は整数で指定してください")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{label}が小さすぎます")
    return value


def _validate_note_id(note_id: Any) -> int:
    value = _validate_int(note_id, "メモ id")
    if not -_INT64_MAX - 1 <= value <= _INT64_MAX:
        # SQLite の整数に収まらない id は存在しえない
        raise NotFound(_NOT_FOUND_MSG)
    return value


def _validate_q(q: Any) -> str:
    return _check_text(q, "検索語", MAX_QUERY)


# ---------------------------------------------------------------- 共通部品

def _visible_clause(user: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """可視条件の唯一の定義（組織が同じ、かつ公開か自分のメモ）。"""
    return "org_id = ? AND (private = 0 OR owner_id = ?)", (user["org_id"], user["id"])


def _load_visible(conn: sqlite3.Connection, user: dict[str, Any], note_id: int) -> dict[str, Any]:
    clause, args = _visible_clause(user)
    row = conn.execute(f"SELECT * FROM notes WHERE id = ? AND {clause}", (note_id, *args)).fetchone()
    if row is None:
        raise NotFound(_NOT_FOUND_MSG)
    return dict(row)


def _check_can_write(user: dict[str, Any], row: dict[str, Any]) -> None:
    """作成者本人か、同組織の管理者だけが更新・削除できる（組織は可視条件で一致済み）。"""
    if row["owner_id"] == user["id"] or user["role"] == "admin":
        return
    raise Forbidden("このメモを変更する権限がありません")


def _audit(conn: sqlite3.Connection, user: dict[str, Any], action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
        (user["org_id"], user["id"], action, detail),
    )


def _tags_of(conn: sqlite3.Connection, note_id: int) -> list[str]:
    rows = conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,)).fetchall()
    return [r["tag"] for r in rows]


def _insert_tags(conn: sqlite3.Connection, note_id: int, tags: list[str]) -> None:
    conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in tags])


def _mask_key(key: Any) -> str:
    """api_key を伏せる。5 文字以上は末尾 4 文字だけ見せ、それ以下は全て伏せる。"""
    text = str(key)
    if len(text) > 4:
        return "****" + text[-4:]
    return "****"


# ---------------------------------------------------------------- 操作

def create_note(conn: sqlite3.Connection, user: dict[str, Any], title: Any, body: Any,
                tags: Any = (), private: Any = False) -> int:
    title = _validate_title(title)
    body = _validate_body(body)
    tag_list = _validate_tags(tags)
    if not isinstance(private, bool):
        raise ValidationError("private は真偽値で指定してください")
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, version, views, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
            (user["org_id"], user["id"], title, body, int(private), _now()),
        )
        note_id = cur.lastrowid
        assert note_id is not None
        hooks.before_write("create:tags")
        _insert_tags(conn, note_id, tag_list)
        _audit(conn, user, "create", f"note={note_id}")
    # ロールバックされた作成を記録しないよう、COMMIT の後に出す。本文・api_key は出さない
    logger.info("note created id=%s owner=%s", note_id, user["id"])
    return note_id


def get_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: Any) -> dict[str, Any]:
    note_id = _validate_note_id(note_id)
    _load_visible(conn, user, note_id)
    hooks.before_write("view")
    clause, args = _visible_clause(user)
    with _tx(conn):
        # 増分は SQL 内で行い、同時閲覧でも失われないようにする
        cur = conn.execute(f"UPDATE notes SET views = views + 1 WHERE id = ? AND {clause}", (note_id, *args))
        if cur.rowcount != 1:
            raise NotFound(_NOT_FOUND_MSG)
        row = dict(conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())
        row["tags"] = _tags_of(conn, note_id)
    return row


def list_notes(conn: sqlite3.Connection, user: dict[str, Any], page: Any = 1, per_page: Any = 20,
               sort: Any = "updated") -> dict[str, Any]:
    page = _validate_int(page, "page", 1)
    per_page = _validate_int(per_page, "per_page", 1)
    orders = {
        "updated": "updated_at DESC, id DESC",
        "title": "title ASC, id ASC",
    }
    if not isinstance(sort, str) or sort not in orders:
        raise ValidationError("sort が不正です")
    clause, args = _visible_clause(user)
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {clause}", args).fetchone()[0]
    offset = (page - 1) * per_page
    items: list[dict[str, Any]] = []
    # 範囲外の page は SQL を発行しない（巨大な値による整数オーバーフローも避ける）
    if offset < total:
        rows = conn.execute(
            f"SELECT id, title, updated_at FROM notes WHERE {clause} ORDER BY {orders[sort]} LIMIT ? OFFSET ?",
            (*args, min(per_page, total), offset),
        ).fetchall()
        items = [dict(r) for r in rows]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


def search_notes(conn: sqlite3.Connection, user: dict[str, Any], q: Any, tags: Any = ()) -> list[dict[str, Any]]:
    q = _validate_q(q)
    tag_list = _validate_tags(tags)
    clause, args = _visible_clause(user)
    sql = f"SELECT id, title FROM notes WHERE {clause}"
    params: list[Any] = list(args)
    if q:
        # instr は % _ ' \ を特別扱いしない（大文字小文字は区別する）
        sql += " AND (instr(title, ?) > 0 OR instr(body, ?) > 0)"
        params += [q, q]
    if tag_list:
        marks = ",".join("?" * len(tag_list))
        sql += (f" AND id IN (SELECT note_id FROM note_tags WHERE tag IN ({marks}) "
                "GROUP BY note_id HAVING COUNT(DISTINCT tag) = ?)")
        params += [*tag_list, len(tag_list)]
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: Any, version: Any,
                title: Any = None, body: Any = None, tags: Any = None) -> int:
    note_id = _validate_note_id(note_id)
    version = _validate_int(version, "version")
    new_title = _validate_title(title) if title is not None else None
    new_body = _validate_body(body) if body is not None else None
    tag_list = _validate_tags(tags) if tags is not None else None
    row = _load_visible(conn, user, note_id)
    _check_can_write(user, row)
    if row["version"] != version:
        raise Conflict("他の変更と競合しました")
    hooks.before_write("update")
    with _tx(conn):
        # フック中の削除・引き継ぎ・非公開化に備えて、可視と権限を再確認する
        current = _load_visible(conn, user, note_id)
        _check_can_write(user, current)
        # version の一致は WHERE が最終的に判定する（確認と書き込みの間の割り込み対策）
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND org_id = ? AND version = ?",
            (new_title if new_title is not None else current["title"],
             new_body if new_body is not None else current["body"],
             _now(), note_id, user["org_id"], version),
        )
        if cur.rowcount != 1:
            raise Conflict("他の変更と競合しました")
        if tag_list is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            _insert_tags(conn, note_id, tag_list)
        _audit(conn, user, "update", f"note={note_id}")
    return version + 1


def delete_note(conn: sqlite3.Connection, user: dict[str, Any], note_id: Any) -> None:
    note_id = _validate_note_id(note_id)
    row = _load_visible(conn, user, note_id)
    _check_can_write(user, row)
    with _tx(conn):
        current = _load_visible(conn, user, note_id)
        _check_can_write(user, current)
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        cur = conn.execute("DELETE FROM notes WHERE id = ? AND org_id = ?", (note_id, user["org_id"]))
        if cur.rowcount != 1:
            raise NotFound(_NOT_FOUND_MSG)
        _audit(conn, user, "delete", f"note={note_id}")


def _require_admin(user: dict[str, Any]) -> None:
    if user["role"] != "admin":
        raise Forbidden("管理者のみ実行できます")


def _resolve_target(conn: sqlite3.Connection, admin: dict[str, Any], ref: Any) -> dict[str, Any]:
    """引き継ぎ元・先を DB から引き直す（呼び出し側 dict の org_id は信用しない）。"""
    if isinstance(ref, dict):
        user_id = ref.get("id")
    else:
        user_id = ref
    user_id = _validate_int(user_id, "利用者 id")
    if not -_INT64_MAX - 1 <= user_id <= _INT64_MAX:
        raise NotFound("利用者が見つかりません")
    found = db.get_user(conn, user_id)
    if found is None:
        raise NotFound("利用者が見つかりません")
    if isinstance(ref, dict) and ref.get("org_id") != found["org_id"]:
        raise Forbidden("別の組織の利用者は指定できません")
    if found["org_id"] != admin["org_id"]:
        raise Forbidden("別の組織の利用者は指定できません")
    return found


def transfer_notes(conn: sqlite3.Connection, admin: dict[str, Any], from_user: Any, to_user: Any) -> int:
    _require_admin(admin)
    from_id = _validate_int(from_user.get("id") if isinstance(from_user, dict) else from_user, "利用者 id")
    to_id = _validate_int(to_user.get("id") if isinstance(to_user, dict) else to_user, "利用者 id")
    if from_id == to_id:
        raise ValidationError("引き継ぎ元と先が同じです")
    with _tx(conn):
        src = _resolve_target(conn, admin, from_user)
        dst = _resolve_target(conn, admin, to_user)
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
            (src["id"], admin["org_id"])).fetchall()]
        for note_id in ids:
            hooks.before_write(f"transfer:{note_id}")
            cur = conn.execute(
                "UPDATE notes SET owner_id = ?, version = version + 1 "
                "WHERE id = ? AND owner_id = ? AND org_id = ?",
                (dst["id"], note_id, src["id"], admin["org_id"]),
            )
            if cur.rowcount != 1:
                raise Conflict("引き継ぎ中に変更が競合しました")
        _audit(conn, admin, "transfer", f"from={src['id']} to={dst['id']} count={len(ids)}")
    return len(ids)


def export_audit(conn: sqlite3.Connection, admin: dict[str, Any]) -> list[dict[str, Any]]:
    _require_admin(admin)
    rows = conn.execute(
        "SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a "
        "LEFT JOIN users u ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id",
        (admin["org_id"],),
    ).fetchall()
    result = []
    for r in rows:
        # 元の api_key は取り出した直後に伏せ、結果へ入れない
        key = _mask_key(r["api_key"]) if r["api_key"] is not None else "****"
        result.append({"id": r["id"], "action": r["action"], "detail": r["detail"],
                       "actor": r["name"], "actor_key": key})
    return result
