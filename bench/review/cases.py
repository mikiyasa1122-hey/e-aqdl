"""欠陥検出ベンチマークのケース（正解データ）。レビュー担当には渡さない。

各ケースは、参照コード（app/）への置き換え（file, old, new）の列。
- 欠陥ケース: repro は欠陥を再現するテスト（参照コードでは合格し、欠陥版では失敗する）
- 欠陥なしケース: 振る舞いを変えない変更。すべての repro が合格する
check_cases.py が、置き換えの適用・見えるテストの合格・repro の合否を機械で確かめる。
"""

S = "notes/service.py"
RL = "notes/ratelimit.py"
SH = "notes/share.py"

# repro の共通の前置き（tests/ の外で実行する）
REPRO_HEAD = '''import logging
import threading
import unittest

from notes import db, service, share
from notes.errors import Conflict, Forbidden, NotFound, RateLimited, ValidationError
from notes.ratelimit import RateLimiter


class R(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.alice = db.add_user(self.conn, 1, 10, "alice", api_key="ak-alice-0001")
        self.bob = db.add_user(self.conn, 2, 10, "bob", api_key="ak-bob-0002")
        self.admin = db.add_user(self.conn, 3, 10, "root", role="admin", api_key="ak-root-0003")
        self.eve = db.add_user(self.conn, 9, 99, "eve", api_key="ak-eve-0009")
        service.hooks.before_write = lambda step: None

    def tearDown(self):
        service.hooks.before_write = lambda step: None

'''


def C(case_id, category, severity, title, location, cause, impact, changes, repro):
    return {"id": case_id, "defect": True, "category": category, "severity": severity, "title": title,
            "location": location, "cause": cause, "impact": impact, "changes": changes, "repro": repro}


def OK(case_id, title, changes):
    return {"id": case_id, "defect": False, "category": "none", "severity": None, "title": title,
            "location": None, "cause": None, "impact": None, "changes": changes, "repro": None}


CASES = [
    # ------------------------------------------------------------------ 認可・スコープ
    C("AUTH-01", "authz", "high", "他の組織のメモを読める・変えられる",
      "notes/service.py _visible", "_visible が組織の一致を確かめていない",
      "他の組織の利用者が、公開メモを読み・更新・削除できる",
      [(S, 'return row is not None and row["org_id"] == user["org_id"] and (not row["private"]',
        'return row is not None and (not row["private"]')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.eve, nid)
'''),
    C("AUTH-02", "authz", "high", "一覧に他人の非公開メモが出る",
      "notes/service.py list_notes", "一覧の条件に非公開メモの除外がない",
      "同じ組織の他人の非公開メモの題名が一覧に出る",
      [(S, '''    where = "org_id = ? AND (private = 0 OR owner_id = ?)"
    args = (user["org_id"], user["id"])''', '''    where = "org_id = ?"
    args = (user["org_id"],)''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "secret", "b", private=True)
        self.assertEqual(service.list_notes(self.conn, self.bob)["total"], 0)
'''),
    C("AUTH-03", "authz", "high", "作成者でも管理者でもない人が公開メモを変えられる",
      "notes/service.py _can_modify", "公開メモなら誰でも変更できる条件が入っている",
      "同じ組織の一般の利用者が、他人の公開メモを更新・削除できる",
      [(S, '''    return row["owner_id"] == user["id"] or (user["role"] == "admin" and row["org_id"] == user["org_id"])''',
        '''    return row["owner_id"] == user["id"] or not row["private"] or (
        user["role"] == "admin" and row["org_id"] == user["org_id"])''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.bob, nid, 1, body="x")
'''),
    C("AUTH-04", "authz", "high", "他の組織の利用者にメモを移せる",
      "notes/service.py transfer_notes", "移し先の利用者の組織を確かめていない",
      "管理者が、自分の組織のメモを他の組織の利用者に渡せる（情報の流出）",
      [(S, '''    if not (from_user["org_id"] == to_user["org_id"] == admin["org_id"]):''',
        '''    if from_user["org_id"] != admin["org_id"]:''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "a", "b")
        with self.assertRaises(Forbidden):
            service.transfer_notes(self.conn, self.admin, self.alice, self.eve)
'''),
    C("AUTH-05", "authz", "high", "検索で他人の非公開メモが見つかる",
      "notes/service.py search_notes", "検索の条件に非公開メモの除外がない",
      "同じ組織の他人の非公開メモが検索結果に出る",
      [(S, '''    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\\\' OR body LIKE ? ESCAPE '\\\\')")''',
        '''    sql = ("SELECT id, title FROM notes WHERE org_id = ? "
           "AND (title LIKE ? ESCAPE '\\\\' OR body LIKE ? ESCAPE '\\\\')")'''),
       (S, '''    args = [user["org_id"], user["id"], pattern, pattern]''', '''    args = [user["org_id"], pattern, pattern]''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "secret plan", "b", private=True)
        self.assertEqual(service.search_notes(self.conn, self.bob, "secret"), [])
'''),
    # ------------------------------------------------------------------ 入力検証
    C("VAL-01", "validation", "medium", "空白だけの題名を受け付ける",
      "notes/service.py _validate", "題名の空白の判定（strip）がない", "空白だけの題名のメモができる",
      [(S, '''if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:''',
        '''if not isinstance(title, str) or not title or len(title) > TITLE_MAX:''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "   ", "b")
'''),
    C("VAL-02", "validation", "medium", "タグの個数の上限を確かめていない",
      "notes/service.py _validate", "タグの個数の検査がない", "6個以上のタグを付けられる",
      [(S, '''    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    for t in tags:''', '''    for t in tags:''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "a", "b", ["t1", "t2", "t3", "t4", "t5", "t6"])
'''),
    C("VAL-03", "validation", "medium", "タグの文字種の検査が先頭だけ",
      "notes/service.py TAG_RE", "正規表現の末尾の $ がなく、先頭が合えば通る", "空白・記号を含むタグを付けられる",
      [(S, '''TAG_RE = re.compile(r"^[a-z0-9-]{1,20}$")''', '''TAG_RE = re.compile(r"^[a-z0-9-]{1,20}")''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "a", "b", ["ok tag!"])
'''),
    C("VAL-04", "validation", "medium", "更新のときにタグを検査しない",
      "notes/service.py update_note", "更新で新しいタグを _validate に渡していない", "更新で不正なタグを付けられる",
      [(S, '''    _validate(new_title, new_body, new_tags if new_tags is not None else [])''',
        '''    _validate(new_title, new_body, [])''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        with self.assertRaises(ValidationError):
            service.update_note(self.conn, self.alice, nid, 1, tags=["Bad Tag"])
'''),
    # ------------------------------------------------------------------ 例外処理
    C("EXC-01", "exception", "medium", "他人の非公開メモの存在が分かる",
      "notes/service.py get_note", "見えないメモに Forbidden を返し、存在しないメモと区別できる",
      "非公開メモ・他の組織のメモの存在を、エラーの種類で推測できる",
      [(S, '''    row = _row(conn, note_id)
    if not _visible(user, row):
        raise NotFound(f"note {note_id}")  # 他の組織・他人の非公開メモは、存在も明かさない''',
        '''    row = _row(conn, note_id)
    if row is None:
        raise NotFound(f"note {note_id}")
    if not _visible(user, row):
        raise Forbidden(f"note {note_id} を見る権限がない")''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b", private=True)
        with self.assertRaises(NotFound):
            service.get_note(self.conn, self.bob, nid)
'''),
    C("EXC-02", "exception", "high", "削除の失敗を握りつぶす",
      "notes/service.py delete_note", "削除の例外を捕まえて警告のログだけにしている",
      "削除に失敗しても呼び出し側は成功したと思う（メモが残る）",
      [(S, '''    with transaction(conn):
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")''',
        '''    try:
        with transaction(conn):
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            hooks.before_write("delete:note")
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            _audit(conn, user, "delete", f"note={note_id}")
    except Exception as e:
        log.warning("delete failed id=%s: %s", note_id, e)''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")

        def boom(step):
            if step == "delete:note":
                raise RuntimeError("disk")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
'''),
    C("EXC-03", "exception", "medium", "形の崩れた共有リンクで None を返す",
      "notes/share.py verify_link", "分解の失敗を None で返し、Forbidden にしていない",
      "呼び出し側が None を note_id として扱い、検証をすり抜けるおそれがある",
      [(SH, '''    except ValueError:
        raise Forbidden("不正なリンク") from None''', '''    except ValueError:
        return None''')],
      '''    def test(self):
        with self.assertRaises(Forbidden):
            share.verify_link("s3cret", "garbage", now=1000)
'''),
    # ------------------------------------------------------------------ 境界値
    C("BND-01", "boundary", "medium", "1ページの件数の上限が51",
      "notes/service.py list_notes", "per_page の上限の比較が1つずれている", "51件を1ページで取れる",
      [(S, '''not 1 <= per_page <= PER_PAGE_MAX:''', '''not 1 <= per_page <= PER_PAGE_MAX + 1:''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.list_notes(self.conn, self.alice, per_page=51)
'''),
    C("BND-02", "boundary", "medium", "page=0 を受け付ける",
      "notes/service.py list_notes", "page の下限の比較が 0 を許している", "page=0 で負の OFFSET になる",
      [(S, '''if not isinstance(page, int) or page < 1:''', '''if not isinstance(page, int) or page < 0:''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.list_notes(self.conn, self.alice, page=0)
'''),
    C("BND-03", "boundary", "medium", "時間枠のちょうど終わりで制限が解けない",
      "notes/ratelimit.py RateLimiter.hit", "枠の終わりの比較が > で、ちょうど window 秒後を枠の中と数える",
      "ちょうど枠の時刻に、余分に1回制限される",
      [(RL, '''            if now - start >= self.window:''', '''            if now - start > self.window:''')],
      '''    def test(self):
        t = [0.0]
        rl = RateLimiter(limit=1, window_sec=60, clock=lambda: t[0])
        rl.hit(1)
        t[0] = 60
        rl.hit(1)
'''),
    C("BND-04", "boundary", "medium", "期限ちょうどの共有リンクが有効",
      "notes/share.py verify_link", "期限の比較が > で、期限の時刻ちょうどを有効にしている", "期限の時刻ちょうどでリンクが使える",
      [(SH, '''    if int(now if now is not None else time.time()) >= expires:''',
        '''    if int(now if now is not None else time.time()) > expires:''')],
      '''    def test(self):
        link = share.make_link("s3cret", 7, ttl_sec=60, now=1000)
        with self.assertRaises(Forbidden):
            share.verify_link("s3cret", link, now=1060)
'''),
    # ------------------------------------------------------------------ 並行・競合
    C("RACE-01", "concurrency", "high", "同時の更新で先の更新が失われる",
      "notes/service.py update_note", "版の比較を読み出し時にだけ行い、UPDATE の条件に版がない",
      "読み出しと書き込みの間に他の更新が入ると、それを上書きして消す（楽観的ロックの破れ）",
      [(S, '''    hooks.before_write("update")
    with transaction(conn):
        cur = conn.execute("UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
                           "WHERE id = ? AND version = ?", (new_title, new_body, _now(), note_id, version))''',
        '''    if row["version"] != version:
        raise Conflict(f"note {note_id} は他の更新と競合した（version {version} は最新ではない）")
    hooks.before_write("update")
    with transaction(conn):
        cur = conn.execute("UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
                           "WHERE id = ?", (new_title, new_body, _now(), note_id))''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        done = []

        def other(step):
            if step == "update" and not done:
                done.append(1)
                service.update_note(self.conn, self.alice, nid, 1, body="other")
        service.hooks.before_write = other
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, body="mine")
'''),
    C("RACE-02", "concurrency", "medium", "閲覧数の加算が同時の閲覧で失われる",
      "notes/service.py get_note", "閲覧数を読み出してから書き戻している（原子的な加算でない）",
      "同時に閲覧されると閲覧数が少なく記録される",
      [(S, '''    hooks.before_write("view")
    with transaction(conn):
        conn.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (note_id,))''',
        '''    views = row["views"]
    hooks.before_write("view")
    with transaction(conn):
        conn.execute("UPDATE notes SET views = ? WHERE id = ?", (views + 1, note_id))''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        done = []

        def other(step):
            if step == "view" and not done:
                done.append(1)
                service.get_note(self.conn, self.bob, nid)
        service.hooks.before_write = other
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["views"], 2)
'''),
    C("RACE-03", "concurrency", "medium", "回数の制限が同時の呼び出しで効かない",
      "notes/ratelimit.py RateLimiter.hit", "呼び出しのたびに新しいロックを作り、排他になっていない",
      "同時に呼ばれると上限を超えて許してしまう",
      [(RL, '''        with self._lock:''', '''        with threading.Lock():''')],
      '''    def test(self):
        rl = RateLimiter(limit=1, window_sec=60, clock=lambda: 0.0)
        a_in, b_done = threading.Event(), threading.Event()
        results = []

        def hook():
            if threading.current_thread().name == "A":
                a_in.set()
                b_done.wait(0.5)
        rl.hook = hook

        def run(name):
            try:
                rl.hit(1)
                results.append("ok")
            except RateLimited:
                results.append("limited")
            if name == "B":
                b_done.set()
        ta = threading.Thread(target=run, args=("A",), name="A")
        ta.start()
        a_in.wait(1)
        tb = threading.Thread(target=run, args=("B",), name="B")
        tb.start()
        ta.join(2)
        tb.join(2)
        self.assertEqual(sorted(results), ["limited", "ok"])
'''),
    # ------------------------------------------------------------------ トランザクション
    C("TX-01", "transaction", "high", "削除の途中の失敗でタグだけ消える",
      "notes/service.py delete_note", "タグの削除とメモの削除を別々のトランザクションにしている",
      "途中で失敗すると、メモは残りタグだけが消える（不整合）",
      [(S, '''    with transaction(conn):
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")''',
        '''    with transaction(conn):
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    hooks.before_write("delete:note")
    with transaction(conn):
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b", ["x"])

        def boom(step):
            if step == "delete:note":
                raise RuntimeError("disk")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.delete_note(self.conn, self.alice, nid)
        service.hooks.before_write = lambda step: None
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["tags"], ["x"])
'''),
    C("TX-02", "transaction", "high", "移管の途中の失敗で一部だけ移る",
      "notes/service.py transfer_notes", "メモごとにトランザクションを分けている",
      "途中で失敗すると一部のメモだけが移る（全部か何もしないか、の仕様に反する）",
      [(S, '''    with transaction(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                                             (from_user["id"], admin["org_id"]))]
        for i in ids:
            hooks.before_write(f"transfer:{i}")
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?", (to_user["id"], i))
        _audit(conn, admin, "transfer", f"from={from_user['id']} to={to_user['id']} count={len(ids)}")''',
        '''    ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                                         (from_user["id"], admin["org_id"]))]
    for i in ids:
        hooks.before_write(f"transfer:{i}")
        with transaction(conn):
            conn.execute("UPDATE notes SET owner_id = ?, version = version + 1 WHERE id = ?", (to_user["id"], i))
    with transaction(conn):
        _audit(conn, admin, "transfer", f"from={from_user['id']} to={to_user['id']} count={len(ids)}")''')],
      '''    def test(self):
        a = service.create_note(self.conn, self.alice, "a", "b")
        b = service.create_note(self.conn, self.alice, "c", "d")

        def boom(step):
            if step == f"transfer:{b}":
                raise RuntimeError("disk")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        self.assertEqual(service.get_note(self.conn, self.alice, a)["owner_id"], 1)
'''),
    C("TX-03", "transaction", "high", "作成の途中の失敗でタグのないメモが残る",
      "notes/service.py create_note", "メモの挿入をコミットしてからタグを挿入している",
      "タグの挿入で失敗すると、タグのないメモと監査の欠けた状態が残る",
      [(S, '''        note_id = cur.lastrowid
        hooks.before_write("create:tags")
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in sorted(set(tags))])
        _audit(conn, user, "create", f"note={note_id}")''',
        '''        note_id = cur.lastrowid
    hooks.before_write("create:tags")
    with transaction(conn):
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in sorted(set(tags))])
        _audit(conn, user, "create", f"note={note_id}")''')],
      '''    def test(self):
        def boom(step):
            if step == "create:tags":
                raise RuntimeError("disk")
        service.hooks.before_write = boom
        with self.assertRaises(RuntimeError):
            service.create_note(self.conn, self.alice, "a", "b", ["x"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)
'''),
    C("TX-04", "transaction", "high", "例外のときにロールバックしない",
      "notes/service.py transaction", "例外のときに ROLLBACK せず、トランザクションが開いたまま残る",
      "一度エラー（競合など）が起きると、その接続の以後の書き込みがすべて失敗する／途中の変更が残る",
      [(S, '''    except BaseException:
        conn.execute("ROLLBACK")
        raise''', '''    except BaseException:
        raise''')],
      '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "a", "b")
        service.update_note(self.conn, self.alice, nid, 1, body="c")
        with self.assertRaises(Conflict):
            service.update_note(self.conn, self.alice, nid, 1, body="stale")
        service.create_note(self.conn, self.alice, "next", "ok")
'''),
    # ------------------------------------------------------------------ SQL の組み立て
    C("SQL-01", "sql", "high", "検索語で SQL を差し込める",
      "notes/service.py search_notes", "検索語を SQL の文字列に直接埋め込んでいる（SQL インジェクション）",
      "他の組織・非公開のメモの読み出しや、条件の改ざんができる",
      [(S, '''    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\\\' OR body LIKE ? ESCAPE '\\\\')")
    pattern = f"%{_like_escape(q)}%"
    args = [user["org_id"], user["id"], pattern, pattern]''',
        '''    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           f"AND (title LIKE '%{q}%' OR body LIKE '%{q}%')")
    args = [user["org_id"], user["id"]]''')],
      '''    def test(self):
        service.create_note(self.conn, self.eve, "other org", "b")
        rows = service.search_notes(self.conn, self.alice, "zz%') OR 1=1 OR ('")
        self.assertEqual(rows, [])
'''),
    C("SQL-02", "sql", "medium", "検索語の % と _ がワイルドカードになる",
      "notes/service.py search_notes", "LIKE の特殊文字をエスケープしていない", "「%」で検索するとすべてのメモが一致する",
      [(S, '''    pattern = f"%{_like_escape(q)}%"''', '''    pattern = f"%{q}%"''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "100% done", "b")
        service.create_note(self.conn, self.alice, "plain", "b")
        self.assertEqual([r["title"] for r in service.search_notes(self.conn, self.alice, "%")], ["100% done"])
'''),
    C("SQL-03", "sql", "high", "並び順の指定で SQL を差し込める",
      "notes/service.py list_notes", "sort を許可した値に限らず、ORDER BY にそのまま入れている",
      "任意の SQL 式を ORDER BY に入れられる（情報の推測・負荷）",
      [(S, '''    if sort not in SORT_COLUMNS:
        raise ValidationError(f"sort は {', '.join(SORT_COLUMNS)} のどれか")
''', ''), (S, '''ORDER BY {SORT_COLUMNS[sort]} "''', '''ORDER BY {SORT_COLUMNS.get(sort, sort)} "''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.list_notes(self.conn, self.alice, sort="(SELECT 1)")
'''),
    C("SQL-04", "sql", "high", "タグの絞り込みで SQL を差し込める",
      "notes/service.py search_notes", "タグを検査せず、文字列として IN 句に埋め込んでいる",
      "タグの値で条件を改ざんできる（他の組織のメモの読み出しなど）",
      [(S, '''        for t in tags:
            if not isinstance(t, str) or not TAG_RE.match(t):
                raise ValidationError(f"不正な tag: {t!r}")
        sql += f" AND id IN (SELECT note_id FROM note_tags WHERE tag IN ({', '.join('?' for _ in tags)}))"
        args += tags''', '''        quoted = ", ".join(f"'{t}'" for t in tags)
        sql += f" AND id IN (SELECT note_id FROM note_tags WHERE tag IN ({quoted}))"''')],
      '''    def test(self):
        with self.assertRaises(ValidationError):
            service.search_notes(self.conn, self.alice, "a", tags=["x') OR 1=1 --"])
'''),
    # ------------------------------------------------------------------ 機密情報
    C("SEC-01", "secret", "medium", "メモの本文をログに出す",
      "notes/service.py create_note", "作成のログに本文を含めている", "非公開メモの本文がログに残る",
      [(S, '''    log.info("note created id=%s owner=%s", note_id, user["id"])''',
        '''    log.info("note created id=%s owner=%s body=%r", note_id, user["id"], body)''')],
      '''    def test(self):
        with self.assertLogs("notes", level="DEBUG") as cm:
            service.create_note(self.conn, self.alice, "a", "TOPSECRET-BODY", private=True)
        self.assertNotIn("TOPSECRET-BODY", "\\n".join(cm.output))
'''),
    C("SEC-02", "secret", "high", "監査ログの出力で api_key を伏せない",
      "notes/service.py export_audit", "api_key をそのまま返している", "利用者の api_key が管理者の出力に全部出る",
      [(S, '''             "actor_key": "*" * len(r["api_key"]) if len(r["api_key"]) < 8 else
             "*" * (len(r["api_key"]) - 4) + r["api_key"][-4:]} for r in rows]''',
        '''             "actor_key": r["api_key"]} for r in rows]''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "a", "b")
        self.assertNotIn("ak-alice", str(service.export_audit(self.conn, self.admin)))
'''),
    C("SEC-03", "secret", "high", "共有リンクの署名をログに出す",
      "notes/share.py make_link", "作成のログに署名（トークン）を含めている", "ログを読める人が共有リンクを作れる",
      [(SH, '''    log.info("share link created note=%s expires=%s", note_id, expires)''',
        '''    log.info("share link created note=%s expires=%s token=%s", note_id, expires, token)''')],
      '''    def test(self):
        with self.assertLogs("notes.share", level="DEBUG") as cm:
            link = share.make_link("s3cret", 7, ttl_sec=60, now=1000)
        self.assertNotIn(link.split(".")[2], "\\n".join(cm.output))
'''),
    # ------------------------------------------------------------------ 欠陥なし（振る舞いを変えない変更）
    OK("CLEAN-01", "読み出しの関数に説明を足し、結果を変数に分ける",
       [(S, '''def _row(conn, note_id):
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()''', '''def _row(conn, note_id):
    """メモの行（なければ None）。認可は呼び出し側で確かめる。"""
    cur = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
    return cur.fetchone()''')]),
    OK("CLEAN-02", "入力検証の順番を入れ替える（タグを先に）",
       [(S, '''    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise ValidationError(f"body は{BODY_MAX}文字まで")
    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")''', '''    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise ValidationError(f"body は{BODY_MAX}文字まで")''')]),
    OK("CLEAN-03", "OFFSET を変数に分ける",
       [(S, '''    rows = conn.execute(f"SELECT id, title, updated_at FROM notes WHERE {where} ORDER BY {SORT_COLUMNS[sort]} "
                        "LIMIT ? OFFSET ?", args + (per_page, (page - 1) * per_page)).fetchall()''',
         '''    offset = (page - 1) * per_page
    rows = conn.execute(f"SELECT id, title, updated_at FROM notes WHERE {where} ORDER BY {SORT_COLUMNS[sort]} "
                        "LIMIT ? OFFSET ?", args + (per_page, offset)).fetchall()''')]),
    OK("CLEAN-04", "IN 句のプレースホルダーの作り方を変える",
       [(S, '''({', '.join('?' for _ in tags)})''', '''({', '.join(['?'] * len(tags))})''')]),
    OK("CLEAN-05", "api_key を伏せる処理を関数に分ける",
       [(S, '''def export_audit(conn, admin):''', '''def _mask(key):
    return "*" * len(key) if len(key) < 8 else "*" * (len(key) - 4) + key[-4:]


def export_audit(conn, admin):'''),
        (S, '''             "actor_key": "*" * len(r["api_key"]) if len(r["api_key"]) < 8 else
             "*" * (len(r["api_key"]) - 4) + r["api_key"][-4:]} for r in rows]''',
         '''             "actor_key": _mask(r["api_key"])} for r in rows]''')]),
    OK("CLEAN-06", "共有リンクの検証で現在時刻を先に求める",
       [(SH, '''    if int(now if now is not None else time.time()) >= expires:''',
         '''    current = int(now if now is not None else time.time())
    if current >= expires:''')]),
    OK("CLEAN-07", "回数の制限の内部の名前を変える",
       [(RL, '''        self._counts = {}  # user_id -> (枠の開始時刻, 回数)''', '''        self._windows = {}  # user_id -> (枠の開始時刻, 回数)'''),
        (RL, '''            start, n = self._counts.get(user_id, (now, 0))''', '''            start, n = self._windows.get(user_id, (now, 0))'''),
        (RL, '''            self._counts[user_id] = (start, n + 1)''', '''            self._windows[user_id] = (start, n + 1)''')]),
    OK("CLEAN-08", "削除に説明とログ（ID だけ）を足す",
       [(S, '''def delete_note(conn, user, note_id):
''', '''def delete_note(conn, user, note_id):
    """メモとタグを1つのトランザクションで消す。"""
'''), (S, '''        _audit(conn, user, "delete", f"note={note_id}")
''', '''        _audit(conn, user, "delete", f"note={note_id}")
    log.info("note deleted id=%s by=%s", note_id, user["id"])
''')]),
    OK("CLEAN-09", "タグの行を変数に分ける",
       [(S, '''        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", [(note_id, t) for t in sorted(set(tags))])
        _audit(conn, user, "create", f"note={note_id}")''', '''        tag_rows = [(note_id, t) for t in sorted(set(tags))]
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)", tag_rows)
        _audit(conn, user, "create", f"note={note_id}")''')]),
    OK("CLEAN-10", "移管の対象の取り出しを fetchall にする",
       [(S, '''        ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                                             (from_user["id"], admin["org_id"]))]''',
         '''        rows = conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                            (from_user["id"], admin["org_id"])).fetchall()
        ids = [r["id"] for r in rows]''')]),
]
