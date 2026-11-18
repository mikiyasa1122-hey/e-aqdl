"""並行処理のテスト例の対象: 在庫と予約（SQLite・標準ライブラリだけ）。

- 予約は在庫を1つ減らす。在庫を確かめてから減らすまでの間に、他の処理が在庫を変えることがある（TOCTOU）。
  → 確認と更新を1つの条件付き UPDATE にする（`stock >= ?`）。確認の結果を、書き込みの時点まで信用しない。
- 商品の更新は楽観的排他（version）。古い version での更新は Conflict（lost update を防ぐ）。
- 予約の登録は冪等キーで二重登録を防ぐ（同じキーの再試行は、前の結果を返す）。ユニーク制約で守る。
- 途中で失敗したら、在庫と予約の両方を元に戻す（1つのトランザクション）。
"""
import contextlib
import sqlite3


class Conflict(Exception):
    pass


class OutOfStock(Exception):
    pass


class _Hooks:
    def before_write(self, step):  # テストが差し替えて、処理の順序を決める・途中で失敗させる
        pass


hooks = _Hooks()


def connect(path=":memory:"):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                          stock INTEGER NOT NULL CHECK (stock >= 0), version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS reservations (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL,
                                                 idem_key TEXT NOT NULL UNIQUE);
    """)
    return conn


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    else:
        conn.execute("COMMIT")


def add_item(conn, name, stock):
    with transaction(conn):
        return conn.execute("INSERT INTO items (name, stock, version) VALUES (?, ?, 1)", (name, stock)).lastrowid


def reserve(conn, item_id, idem_key):
    """在庫を1つ減らして予約を作る。同じ idem_key の再試行は、前の予約の id を返す（二重に減らさない）。"""
    row = conn.execute("SELECT id FROM reservations WHERE idem_key = ?", (idem_key,)).fetchone()
    if row:
        return row["id"]
    hooks.before_write("reserve:check")  # 確認の後・書き込みの前（他の処理が割り込む位置）
    try:
        with transaction(conn):
            cur = conn.execute("UPDATE items SET stock = stock - 1, version = version + 1 "
                               "WHERE id = ? AND stock >= 1", (item_id,))
            if cur.rowcount != 1:
                raise OutOfStock(f"item {item_id}")
            hooks.before_write("reserve:insert")  # 在庫を減らした後・予約を作る前（途中の失敗の位置）
            return conn.execute("INSERT INTO reservations (item_id, idem_key) VALUES (?, ?)",
                                (item_id, idem_key)).lastrowid
    except sqlite3.IntegrityError:
        # 同じキーの別の処理が先に登録した（同時の再試行）。その結果を返す
        row = conn.execute("SELECT id FROM reservations WHERE idem_key = ?", (idem_key,)).fetchone()
        if row:
            return row["id"]
        raise


def rename(conn, item_id, version, name):
    """楽観的排他: 呼び出し側が読んだ version が最新でなければ Conflict。"""
    hooks.before_write("rename")
    with transaction(conn):
        cur = conn.execute("UPDATE items SET name = ?, version = version + 1 WHERE id = ? AND version = ?",
                           (name, item_id, version))
        if cur.rowcount != 1:
            raise Conflict(f"item {item_id} version {version}")
    return version + 1


def delete_item(conn, item_id):
    with transaction(conn):
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
