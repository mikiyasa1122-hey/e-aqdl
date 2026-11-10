"""チームのメモ（組織ごと）。認可・入力検証・楽観的ロック・トランザクションの規則は仕様書（spec.md）のとおり。"""
import contextlib
import logging
import re
import sqlite3
import time

from .errors import Conflict, Forbidden, NotFound, ValidationError

log = logging.getLogger("notes")

TITLE_MAX = 100
BODY_MAX = 10000
TAGS_MAX = 5
TAG_RE = re.compile(r"[a-z0-9-]{1,20}")  # fullmatch で使う（末尾の改行を通さない）
PER_PAGE_MAX = 50
MAX_OFFSET = 2 ** 63 - 1  # SQLite の整数の上限。これを超える page は結果なしにする
SORT_COLUMNS = {"updated": "updated_at DESC, id DESC", "title": "title ASC, id ASC"}


class Hooks:
    """テストから並行実行・途中の障害を再現するための差し込み口。本番では何もしない。"""

    def before_write(self, step):
        pass


hooks = Hooks()


def _rollback(conn):
    """ROLLBACK する。ROLLBACK 自体の失敗（SQLite が自動でロールバック済みなど）で、元の例外を隠さない。"""
    if not conn.in_transaction:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        log.warning("rollback failed")


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback(conn)
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise


def _now():
    return int(time.time() * 1000)


def _tag_list(tags):
    if not isinstance(tags, (list, tuple)):
        raise ValidationError("tags はタグの列（list か tuple）")
    return list(tags)


def _validate(title, body, tags):
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        raise ValidationError(f"title は1〜{TITLE_MAX}文字")
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise ValidationError(f"body は{BODY_MAX}文字まで")
    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    for t in tags:
        if not isinstance(t, str) or not TAG_RE.fullmatch(t):
            raise ValidationError(f"不正な tag: {t!r}")


def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))


def _visible(user, row):
    """この利用者が見てよいメモか（組織が同じで、非公開なら本人だけ）。"""
    return row is not None and row["org_id"] == user["org_id"] and (not row["private"] or row["owner_id"] == user["id"])


def _can_modify(user, row):
    return row["owner_id"] == user["id"] or (user["role"] == "admin" and row["org_id"] == user["org_id"])


def create_note(conn, user, title, body, tags=(), private=False):
    tags = _tag_list(tags)
    _validate(title, body, tags)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO notes (org_id, owner_id, title, body, private, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["org_id"], user["id"], title, body, 1 if private else 0, _now()))
        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in sorted(set(tags))])
        _audit(conn, user, "create", f"note={note_id}")
    log.info("note created id=%s owner=%s", note_id, user["id"])
    return note_id


def _row(conn, note_id):
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def get_note(conn, user, note_id):
    row = _row(conn, note_id)
    if not _visible(user, row):
        raise NotFound(f"note {note_id}")  # 他の組織・他人の非公開メモは、存在も明かさない
    hooks.before_write("view")
    with transaction(conn):
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))
        row = _row(conn, note_id)
        if not _visible(user, row):  # 確かめた後に削除・変更された
            raise NotFound(f"note {note_id}")
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]
    note = dict(row)
    note["tags"] = tags
    return note


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if isinstance(page, bool) or isinstance(per_page, bool):
        raise ValidationError("page・per_page は整数")
    if not isinstance(page, int) or page < 1:
        raise ValidationError("page は1以上")
    if not isinstance(per_page, int) or not 1 <= per_page <= PER_PAGE_MAX:
        raise ValidationError(f"per_page は1〜{PER_PAGE_MAX}")
    if sort not in SORT_COLUMNS:
        raise ValidationError(f"sort は {', '.join(SORT_COLUMNS)} のどれか")
    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
    args = (user["org_id"], user["id"])
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", args).fetchone()[0]
    offset = (page - 1) * per_page
    rows = [] if offset > MAX_OFFSET else conn.execute(
        f"SELECT id, title, updated_at FROM notes WHERE {where} ORDER BY {SORT_COLUMNS[sort]} LIMIT ? OFFSET ?",
        args + (per_page, offset)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


def _like_escape(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(conn, user, q, tags=()):
    if not isinstance(q, str) or not q.strip() or len(q) > 100:
        raise ValidationError("q は1〜100文字")
    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
    pattern = f"%{_like_escape(q)}%"
    args = [user["org_id"], user["id"], pattern, pattern]
    tags = _tag_list(tags)
    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    if tags:
        for t in tags:
            if not isinstance(t, str) or not TAG_RE.fullmatch(t):
                raise ValidationError(f"不正な tag: {t!r}")
        sql += f" AND id IN (SELECT note_id FROM note_tags WHERE tag IN ({', '.join('?' for _ in tags)}))"
        args += tags
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def _check_modify(user, row, note_id, what):
    if not _visible(user, row):
        raise NotFound(f"note {note_id}")
    if not _can_modify(user, row):
        raise Forbidden(f"{what}できるのは作成者と同じ組織の管理者だけ")


def update_note(conn, user, note_id, version, title=None, body=None, tags=None):
    new_tags = None if tags is None else _tag_list(tags)
    _check_modify(user, _row(conn, note_id), note_id, "更新")  # トランザクションの前に、早めに断る
    hooks.before_write("update")
    with transaction(conn):
        # 認可と入力の検証は、書き込みと同じトランザクションの中で読み直した行で行う（判定の後の移管・変更を見逃さない）
        row = _row(conn, note_id)
        _check_modify(user, row, note_id, "更新")
        new_title = row["title"] if title is None else title
        new_body = row["body"] if body is None else body
        _validate(new_title, new_body, new_tags if new_tags is not None else [])
        cur = conn.execute("UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
                           "WHERE id = ? AND version = ?", (new_title, new_body, _now(), note_id, version))
        if cur.rowcount != 1:
            raise Conflict(f"note {note_id} は他の更新と競合した（version {version} は最新ではない）")
        if new_tags is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                             [(note_id, t) for t in sorted(set(new_tags))])
        _audit(conn, user, "update", f"note={note_id}")
    return version + 1


def delete_note(conn, user, note_id):
    with transaction(conn):
        _check_modify(user, _row(conn, note_id), note_id, "削除")
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")


def transfer_notes(conn, admin, from_user, to_user):
    """from_user のメモをすべて to_user に移す（管理者だけ。同じ組織の中だけ。全部か、何もしないか）。"""
    if admin["role"] != "admin":
        raise Forbidden("管理者だけ")
    if not (from_user["org_id"] == to_user["org_id"] == admin["org_id"]):
        raise Forbidden("同じ組織の中でだけ移せる")
    with transaction(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                                             (from_user["id"], admin["org_id"]))]
        for i in ids:
            hooks.before_write(f"transfer:{i}")
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?", (to_user["id"], i))
        _audit(conn, admin, "transfer", f"from={from_user['id']} to={to_user['id']} count={len(ids)}")
    return len(ids)


def export_audit(conn, admin):
    """組織の監査ログ（管理者だけ）。利用者の api_key は末尾4文字以外を伏せる（8文字未満の鍵はすべて伏せる）。"""
    if admin["role"] != "admin":
        raise Forbidden("管理者だけ")
    rows = conn.execute("SELECT a.id, a.action, a.detail, u.name, u.api_key FROM audit a JOIN users u "
                        "ON u.id = a.actor_id WHERE a.org_id = ? ORDER BY a.id", (admin["org_id"],)).fetchall()
    return [{"id": r["id"], "action": r["action"], "detail": r["detail"], "actor": r["name"],
             "actor_key": "*" * len(r["api_key"]) if len(r["api_key"]) < 8 else
             "*" * (len(r["api_key"]) - 4) + r["api_key"][-4:]} for r in rows]
