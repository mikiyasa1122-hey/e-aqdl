import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('member', 'admin')),
    api_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    views INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER NOT NULL REFERENCES notes(id),
    tag TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def connect(path=":memory:"):
    # 接続はスレッドごとに1つ（共有しない）。書き込みのロック待ちは5秒まで
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def add_user(conn, user_id, org_id, name, role="member", api_key="key"):
    conn.execute("INSERT INTO users (id, org_id, name, role, api_key) VALUES (?, ?, ?, ?, ?)",
                 (user_id, org_id, name, role, api_key))
    return get_user(conn, user_id)


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None
