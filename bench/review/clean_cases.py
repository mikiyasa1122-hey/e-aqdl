"""クリーンケース群（EVAL-01）。レビュー担当には渡さない。

既存の40ケース（cases.py）は、見えるテストがわざと薄く、参照コードに実在の欠陥も残っている。そのため、
「欠陥がなく、検証も揃っているときに正しく合格できるか」を測れない。ここでは、それを測るための別の題材を使う。

- 参照コード: app_clean/。app/ の残っていた実在の欠陥を直したもの
  （タグの末尾の改行・認可の判定がトランザクションの外・巨大な page の OverflowError・ROLLBACK が元の例外を隠す・
  更新で変えない欄を古い行から取る）。
- テスト: app_clean/tests/。受け入れ条件 AC1〜AC10 とリスク K1〜K8 ごとに、正常系・異常系・境界値・途中の失敗・
  同時の操作を確かめる（ファイル名とクラスの説明に対象の ID を書いた）。
- ケース: 参照コードと、振る舞いを変えない書き換え（9件）。どれも欠陥なし・検証あり、を正解とする。

check_cases.py --set clean が、レビューの前に次を機械で確かめる（クリーンであることの独立した確認）。
- 各ケースで、テスト（app_clean/tests）がすべて合格する
- 各ケースで、40ケースの30件の欠陥の repro と、残っていた欠陥の repro（RESIDUAL_REPROS）がすべて合格する
- テストの強さ: 参照コードに30件の欠陥（MUTATIONS。置き換え元が合わないものは同じ欠陥を app_clean に合わせて書いた）を
  1つずつ入れると、テストが失敗する
"""
S = "notes/service.py"
RL = "notes/ratelimit.py"
SH = "notes/share.py"

APP_DIR = "app_clean"


def K(case_id, title, changes):
    return {"id": case_id, "defect": False, "category": "none", "severity": None, "title": title,
            "location": None, "cause": None, "impact": None, "changes": changes, "repro": None, "app": APP_DIR,
            "clean_set": True}


CASES = [
    K("CLN-01", "参照コードそのまま", []),
    K("CLN-02", "入力検証を、題名・本文・タグの関数に分ける",
      [(S, '''def _validate(title, body, tags):
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        raise ValidationError(f"title は1〜{TITLE_MAX}文字")
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise ValidationError(f"body は{BODY_MAX}文字まで")
    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    for t in tags:
        if not isinstance(t, str) or not TAG_RE.fullmatch(t):
            raise ValidationError(f"不正な tag: {t!r}")
''', '''def _validate_title(title):
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        raise ValidationError(f"title は1〜{TITLE_MAX}文字")


def _validate_body(body):
    if not isinstance(body, str) or len(body) > BODY_MAX:
        raise ValidationError(f"body は{BODY_MAX}文字まで")


def _validate_tags(tags):
    """タグの数（重複も数える）と、各タグの形。"""
    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    for t in tags:
        if not isinstance(t, str) or not TAG_RE.fullmatch(t):
            raise ValidationError(f"不正な tag: {t!r}")


def _validate(title, body, tags):
    _validate_title(title)
    _validate_body(body)
    _validate_tags(tags)
'''),
       (S, '''    tags = _tag_list(tags)
    if len(tags) > TAGS_MAX:
        raise ValidationError(f"tags は{TAGS_MAX}個まで")
    if tags:
        for t in tags:
            if not isinstance(t, str) or not TAG_RE.fullmatch(t):
                raise ValidationError(f"不正な tag: {t!r}")
        sql +=''', '''    tags = _tag_list(tags)
    _validate_tags(tags)
    if tags:
        sql +=''')]),
    K("CLN-03", "api_key を伏せる処理を関数に分ける",
      [(S, '''def export_audit(conn, admin):''', '''def _mask_key(key):
    """末尾4文字以外を * にする。8文字未満の鍵はすべて伏せる。"""
    if len(key) < 8:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def export_audit(conn, admin):'''),
       (S, '''             "actor_key": "*" * len(r["api_key"]) if len(r["api_key"]) < 8 else
             "*" * (len(r["api_key"]) - 4) + r["api_key"][-4:]} for r in rows]''',
        '''             "actor_key": _mask_key(r["api_key"])} for r in rows]''')]),
    K("CLN-04", "一覧の入力検証を関数に分ける",
      [(S, '''def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    if isinstance(page, bool) or isinstance(per_page, bool):
        raise ValidationError("page・per_page は整数")
    if not isinstance(page, int) or page < 1:
        raise ValidationError("page は1以上")
    if not isinstance(per_page, int) or not 1 <= per_page <= PER_PAGE_MAX:
        raise ValidationError(f"per_page は1〜{PER_PAGE_MAX}")
    if sort not in SORT_COLUMNS:
        raise ValidationError(f"sort は {', '.join(SORT_COLUMNS)} のどれか")
''', '''def _int_in(value, low, high, name):
    """bool を除く整数で、low 以上（high があれば high 以下）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} は整数")
    if value < low or (high is not None and value > high):
        raise ValidationError(f"{name} は{low}〜{high if high is not None else ''}")
    return value


def list_notes(conn, user, page=1, per_page=20, sort="updated"):
    _int_in(page, 1, None, "page")
    _int_in(per_page, 1, PER_PAGE_MAX, "per_page")
    if sort not in SORT_COLUMNS:
        raise ValidationError(f"sort は {', '.join(SORT_COLUMNS)} のどれか")
''')]),
    K("CLN-05", "メモのタグの読み出しを関数に分ける",
      [(S, '''def get_note(conn, user, note_id):''', '''def _tags_of(conn, note_id):
    return [r["tag"] for r in conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]


def get_note(conn, user, note_id):'''),
       (S, '''        tags = [r["tag"] for r in conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,))]''',
        '''        tags = _tags_of(conn, note_id)''')]),
    K("CLN-06", "回数の制限の記録を、利用者ごとの小さなクラスにする",
      [(RL, '''class RateLimiter:''', '''class _Window:
    __slots__ = ("start", "count")

    def __init__(self, start):
        self.start = start
        self.count = 0


class RateLimiter:'''),
       (RL, '''        self._counts = {}  # user_id -> (枠の開始時刻, 回数)''', '''        self._windows = {}  # user_id -> _Window'''),
       (RL, '''            now = self.clock()
            start, n = self._counts.get(user_id, (now, 0))
            if now - start >= self.window:
                start, n = now, 0
            self.hook()
            if n >= self.limit:
                raise RateLimited(f"user {user_id}: {self.window}秒に{self.limit}回まで")
            self._counts[user_id] = (start, n + 1)
            return self.limit - n - 1''', '''            now = self.clock()
            w = self._windows.get(user_id)
            if w is None or now - w.start >= self.window:
                w = _Window(now)
            self.hook()
            if w.count >= self.limit:
                raise RateLimited(f"user {user_id}: {self.window}秒に{self.limit}回まで")
            w.count += 1
            self._windows[user_id] = w
            return self.limit - w.count''')]),
    K("CLN-07", "共有リンクの検証を、例外を使わない順の確認に書き換える",
      [(SH, '''    if not isinstance(link, str):
        raise Forbidden("不正なリンク")
    try:
        m = LINK_RE.fullmatch(link)
        if not m:
            raise ValueError(link)
        note_id, expires, token = int(m.group(1)), int(m.group(2)), m.group(3)
    except ValueError:
        raise Forbidden("不正なリンク") from None
''', '''    m = LINK_RE.fullmatch(link) if isinstance(link, str) else None
    if m is None:
        raise Forbidden("不正なリンク")
    note_id, expires, token = int(m.group(1)), int(m.group(2)), m.group(3)
''')]),
    K("CLN-08", "移管の対象の読み出しを、変数に分けて書く",
      [(S, '''        ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                                             (from_user["id"], admin["org_id"]))]''',
        '''        rows = conn.execute("SELECT id FROM notes WHERE owner_id = ? AND org_id = ? ORDER BY id",
                            (from_user["id"], admin["org_id"])).fetchall()
        ids = [r["id"] for r in rows]''')]),
    K("CLN-09", "検索の SQL の組み立てを、条件の列から作る",
      [(S, '''    sql = ("SELECT id, title FROM notes WHERE org_id = ? AND (private = 0 OR owner_id = ?) "
           "AND (title LIKE ? ESCAPE '\\\\' OR body LIKE ? ESCAPE '\\\\')")
    pattern = f"%{_like_escape(q)}%"
    args = [user["org_id"], user["id"], pattern, pattern]''',
        '''    pattern = f"%{_like_escape(q)}%"
    conditions = ["org_id = ?", "(private = 0 OR owner_id = ?)", "(title LIKE ? ESCAPE '\\\\' OR body LIKE ? ESCAPE '\\\\')"]
    args = [user["org_id"], user["id"], pattern, pattern]
    sql = "SELECT id, title FROM notes WHERE " + " AND ".join(conditions)''')]),
    K("CLN-10", "監査ログの記録に、操作ごとの関数名を使う（記録の内容は同じ）",
      [(S, '''def _audit(conn, user, action, detail):
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))''', '''AUDIT_ACTIONS = ("create", "update", "delete", "transfer")


def _audit(conn, user, action, detail):
    """監査ログに1行足す（呼び出し側のトランザクションの中で）。detail には ID だけを書く。"""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action: {action}")
    conn.execute("INSERT INTO audit (org_id, actor_id, action, detail) VALUES (?, ?, ?, ?)",
                 (user["org_id"], user["id"], action, detail))''')]),
]

# 残っていた欠陥（v0.6.6 の基準値の ANALYSIS.md）の repro。app_clean では合格し、app では失敗する
RESIDUAL_REPROS = {
    "RES-TAG-NEWLINE": '''    def test(self):
        with self.assertRaises(ValidationError):
            service.create_note(self.conn, self.alice, "t", "b", ["abc\\n"])
''',
    "RES-AUTHZ-TOCTOU": '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def transfer(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.transfer_notes(self.conn, self.admin, self.alice, self.bob)
        service.hooks.before_write = transfer
        with self.assertRaises(Forbidden):
            service.update_note(self.conn, self.alice, nid, 2, body="x")
''',
    "RES-PAGE-OVERFLOW": '''    def test(self):
        self.assertEqual(service.list_notes(self.conn, self.alice, page=10 ** 30)["items"], [])
''',
    "RES-ROLLBACK-MASK": '''    def test(self):
        class Boom(Exception):
            pass

        def end_txn(step):
            if step == "create:tags":
                self.conn.execute("ROLLBACK")
                raise Boom()
        service.hooks.before_write = end_txn
        with self.assertRaises(Boom):
            service.create_note(self.conn, self.alice, "t", "b")
''',
    "RES-STALE-FIELDS": '''    def test(self):
        nid = service.create_note(self.conn, self.alice, "t", "b")

        def other(step):
            if step == "update":
                service.hooks.before_write = lambda s: None
                service.update_note(self.conn, self.admin, nid, 1, body="other")
        service.hooks.before_write = other
        service.update_note(self.conn, self.alice, nid, 2, title="mine")
        self.assertEqual(service.get_note(self.conn, self.alice, nid)["body"], "other")
''',
}

# テストの強さの確認に使う欠陥。cases.py の置き換えが app_clean に合わないものだけ、同じ欠陥を app_clean に合わせて書く
MUTATIONS_ADAPTED = {
    "VAL-03": [(S, 'TAG_RE = re.compile(r"[a-z0-9-]{1,20}")', 'TAG_RE = re.compile(r"[a-z0-9-]{1,20}.*", re.S)')],
    "EXC-02": [(S, '''    with transaction(conn):
        _check_modify(user, _row(conn, note_id), note_id, "削除")
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        _audit(conn, user, "delete", f"note={note_id}")''', '''    _check_modify(user, _row(conn, note_id), note_id, "削除")
    try:
        with transaction(conn):
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            hooks.before_write("delete:note")
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            _audit(conn, user, "delete", f"note={note_id}")
    except Exception as e:
        log.warning("delete failed id=%s: %s", note_id, e)''')],
    "RACE-01": [(S, '"WHERE id = ? AND version = ?", (new_title, new_body, _now(), note_id, version))',
                 '"WHERE id = ?", (new_title, new_body, _now(), note_id))')],
    "TX-01": [(S, '''        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        hooks.before_write("delete:note")
        conn.execute("DELETE FROM notes''', '''        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    hooks.before_write("delete:note")
    with transaction(conn):
        conn.execute("DELETE FROM notes''')],
    "TX-04": [(S, '''    except BaseException:
        _rollback(conn)
        raise
    else:''', '''    except BaseException:
        raise
    else:''')],
    "SQL-03": [(S, '''    if sort not in SORT_COLUMNS:
        raise ValidationError(f"sort は {', '.join(SORT_COLUMNS)} のどれか")
''', ''), (S, "ORDER BY {SORT_COLUMNS[sort]}", "ORDER BY {SORT_COLUMNS.get(sort, sort)}")],
    "SQL-04": [(S, '''        for t in tags:
            if not isinstance(t, str) or not TAG_RE.fullmatch(t):
                raise ValidationError(f"不正な tag: {t!r}")
        sql += f" AND id IN (SELECT note_id FROM note_tags WHERE tag IN ({', '.join('?' for _ in tags)}))"
        args += tags''', '''        quoted = ", ".join(f"'{t}'" for t in tags)
        sql += f" AND id IN (SELECT note_id FROM note_tags WHERE tag IN ({quoted}))"''')],
}
