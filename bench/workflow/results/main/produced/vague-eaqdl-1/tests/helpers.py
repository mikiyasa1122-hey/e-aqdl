"""テスト共通の部品。DB は :memory: か一時フォルダのファイル DB だけを使う。"""
import os
import tempfile
import unittest

from notes import db, service
from notes.errors import Conflict, Forbidden, NotFound

# 目立つ偽の api_key（実在の資格情報ではない）
KEYS = {
    1: "FAKE-KEY-ORG1-ADMIN-aaaa1111",
    2: "FAKE-KEY-ORG1-MEMBER2-bbbb2222",
    3: "FAKE-KEY-ORG1-MEMBER3-cccc3333",
    11: "FAKE-KEY-ORG2-ADMIN-dddd4444",
    12: "FAKE-KEY-ORG2-MEMBER-eeee5555",
}


def add_all_users(conn):
    """組織1（admin 1・member 2, 3）と組織2（admin 11・member 12）を作り、dict を返す。"""
    users = {}
    for uid, org, name, role in [(1, 1, "a1", "admin"), (2, 1, "a2", "member"), (3, 1, "a3", "member"),
                                 (11, 2, "b1", "admin"), (12, 2, "b2", "member")]:
        users[uid] = db.add_user(conn, uid, org, name, role, KEYS[uid])
    return users


def make_env():
    conn = db.connect(":memory:")
    return conn, add_all_users(conn)


def make_file_env(tc):
    """ファイル DB を作り (path, conn1, conn2, users) を返す。後始末は tc に登録する。"""
    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "notes.sqlite3")
    conn1 = db.connect(path)
    conn2 = db.connect(path)
    tc.addCleanup(conn2.close)
    tc.addCleanup(conn1.close)
    return path, conn1, conn2, add_all_users(conn1)


def set_hook(tc, func):
    """service.hooks.before_write を差し替え、テスト終了時に元へ戻す。"""
    original = service.hooks.__dict__.get("before_write")
    service.hooks.before_write = func

    def restore():
        if original is None:
            del service.hooks.before_write
        else:
            service.hooks.before_write = original
    tc.addCleanup(restore)


def snapshot(conn):
    """3 テーブルの全行（「何も変わらない」の確認用）。"""
    return {
        "notes": [tuple(r) for r in conn.execute("SELECT * FROM notes ORDER BY id")],
        "note_tags": [tuple(r) for r in conn.execute("SELECT * FROM note_tags ORDER BY note_id, tag")],
        "audit": [tuple(r) for r in conn.execute("SELECT * FROM audit ORDER BY id")],
    }


def orphan_tags(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM note_tags WHERE note_id NOT IN (SELECT id FROM notes)").fetchone()[0]


# --- 変異テストからも呼ぶ検証本体（欠陥があれば AssertionError で失敗する） ---

def check_org_isolation(tc):
    """V1: 組織Bのメモが組織Aの利用者に見えず触れない。"""
    conn, u = make_env()
    b_id = service.create_note(conn, u[12], "org-b-note", "org-b-body", tags=["t"])
    service.create_note(conn, u[2], "org-a-note", "org-a-body", tags=["t"])
    for user in (u[2], u[1]):  # member と管理者のどちらでも
        with tc.assertRaises(NotFound):
            service.get_note(conn, user, b_id)
        with tc.assertRaises(NotFound):
            service.update_note(conn, user, b_id, 1, title="x")
        with tc.assertRaises(NotFound):
            service.delete_note(conn, user, b_id)
        listed = service.list_notes(conn, user)
        tc.assertEqual(listed["total"], 1)
        tc.assertNotIn(b_id, [i["id"] for i in listed["items"]])
        tc.assertNotIn(b_id, [i["id"] for i in service.search_notes(conn, user, "org-b")])
        tc.assertEqual(service.search_notes(conn, user, "note", tags=["t"]), [{"id": 2, "title": "org-a-note"}])
    # 触れていないこと
    row = conn.execute("SELECT title, version, views FROM notes WHERE id = ?", (b_id,)).fetchone()
    tc.assertEqual(tuple(row), ("org-b-note", 1, 0))


def check_update_conflict(tc):
    """V30: hooks 'update' で他接続が先に更新を確定させたら Conflict、先の更新が残る。"""
    path, conn1, conn2, u = make_file_env(tc)
    nid = service.create_note(conn1, u[2], "orig", "body")
    state = {"done": False}

    def hook(step):
        if step == "update" and not state["done"]:
            state["done"] = True
            service.update_note(conn2, u[2], nid, 1, title="first-writer")
    set_hook(tc, hook)
    with tc.assertRaises(Conflict):
        service.update_note(conn1, u[2], nid, 1, title="second-writer")
    row = conn1.execute("SELECT title, version FROM notes WHERE id = ?", (nid,)).fetchone()
    tc.assertEqual(tuple(row), ("first-writer", 2))
    tc.assertFalse(conn1.in_transaction)
