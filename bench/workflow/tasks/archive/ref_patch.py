"""アーカイブ機能の参照実装（app_clean への置き換え）。隠しテストが正しいことを確かめるためだけに使う。"""
D = "notes/db.py"
S = "notes/service.py"
CHANGES = [
    (D, "    private INTEGER NOT NULL DEFAULT 0,\n", "    private INTEGER NOT NULL DEFAULT 0,\n    archived INTEGER NOT NULL DEFAULT 0,\n"),
    (S, 'def list_notes(conn, user, page=1, per_page=20, sort="updated"):',
     'def list_notes(conn, user, page=1, per_page=20, sort="updated", include_archived=False):'),
    (S, '''    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
''', '''    where = "org_id = ? AND (private = 0 OR owner_id = ?)" + ("" if include_archived else " AND archived = 0")
'''),
    (S, "def search_notes(conn, user, q, tags=()):", "def search_notes(conn, user, q, tags=(), include_archived=False):"),
    (S, '''    pattern = f"%{_like_escape(q)}%"
''', '''    if not include_archived:
        sql += " AND archived = 0"
    pattern = f"%{_like_escape(q)}%"
'''),
    (S, '''        _check_modify(user, row, note_id, "更新")
        new_title''', '''        _check_modify(user, row, note_id, "更新")
        if row["archived"]:
            raise Conflict(f"note {note_id} はアーカイブ済みで更新できない")
        new_title'''),
    (S, '''def delete_note(conn, user, note_id):''', '''def _set_archived(conn, user, note_id, value):
    with transaction(conn):
        row = _row(conn, note_id)
        _check_modify(user, row, note_id, "アーカイブ")
        if row["archived"] == value:
            raise Conflict(f"note {note_id} はすでにその状態")
        conn.execute("UPDATE notes SET archived = ?, version = version + 1 WHERE id = ?", (value, note_id))
        _audit(conn, user, "archive" if value else "unarchive", f"note={note_id}")
        return row["version"] + 1


def archive_note(conn, user, note_id):
    return _set_archived(conn, user, note_id, 1)


def unarchive_note(conn, user, note_id):
    return _set_archived(conn, user, note_id, 0)


def delete_note(conn, user, note_id):'''),
]
