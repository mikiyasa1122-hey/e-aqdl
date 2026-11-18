"""「検証あり・欠陥あり」のケース群。レビュー担当には渡さない。

既存の2つの群では測れない組み合わせを測る。
- 既存の40ケース: 見えるテストが薄い。レビュー担当は疑って読む
- クリーンケース群: テストが揃っていて、欠陥がない
- この群: **テストが揃って見え、すべて合格するが、欠陥が1つある**

全工程の比較の試行（bench/workflow/results/pilot）で、自作のテストがすべて合格している実装に対して、独立レビューが
実在の欠陥を見逃して「合格」にした。テストの合格を、欠陥がない証拠として読んでいないかを確かめる。

作り方: 参照コード（app_clean）に欠陥を1つ入れ、app_clean/tests のうち、その欠陥で失敗するテストのメソッドだけを取り除く
（check_cases.prune_failing_tests）。残りのテスト（50件以上）はすべて合格する。受け入れ条件ごとのファイルは残るので、
検証は揃って見える。取り除いたテストは built/<id>.json の dropped_tests に記録する。
"""
import cases as CS
import clean_cases as CL

S = "notes/service.py"
APP_DIR = "app_clean"


def T(case_id, severity, category, title, location, cause, impact, changes, repro):
    return {"id": case_id, "defect": True, "category": category, "severity": severity, "title": title,
            "location": location, "cause": cause, "impact": impact, "changes": changes, "repro": repro,
            "app": APP_DIR, "tested_set": True, "drop_failing_tests": True}


def from_case(new_id, src_id):
    """既存の40ケースの欠陥を、app_clean に入れる（置き換え元が合わないものは clean_cases.MUTATIONS_ADAPTED を使う）。"""
    c = next(x for x in CS.CASES if x["id"] == src_id)
    return dict(c, id=new_id, source=src_id, changes=CL.MUTATIONS_ADAPTED.get(src_id, c["changes"]),
                app=APP_DIR, tested_set=True, drop_failing_tests=True)


CASES = [
    T("TD-01", "high", "auth", "認可を、書き込みのトランザクションの中で確かめ直していない",
      "notes/service.py update_note",
      "トランザクションの前に認可を確かめた後、トランザクションの中で行を読み直すが、認可は確かめ直さない",
      "判定の後に移管・非公開化されたメモを、元の所有者や管理者が更新できる（K1）",
      [(S, '''        row = _row(conn, note_id)
        _check_modify(user, row, note_id, "更新")
''', '''        row = _row(conn, note_id)
''')], CL.RESIDUAL_REPROS["RES-AUTHZ-TOCTOU"]),
    T("TD-02", "medium", "boundary", "巨大な page で OverflowError になる",
      "notes/service.py list_notes", "OFFSET が SQLite の整数の範囲を超えるかを確かめずに、SQL に渡している",
      "page に極端に大きい整数を渡すと、ValidationError でも空の結果でもなく OverflowError になる（R3・R10）",
      [(S, '''    rows = [] if offset > MAX_OFFSET else conn.execute(''', '''    rows = conn.execute(''')],
      CL.RESIDUAL_REPROS["RES-PAGE-OVERFLOW"]),
    T("TD-03", "high", "race", "更新で変えない欄を、トランザクションの前に読んだ行から取る",
      "notes/service.py update_note",
      "変えない欄（title・body）の値を、トランザクションの前に読んだ行から取り、トランザクションの中で読み直さない",
      "読み出しの後に入った他の更新を、呼び出し側が最新の version を渡したときに消す（R4・K3）",
      [(S, '''    _check_modify(user, _row(conn, note_id), note_id, "更新")  # トランザクションの前に、早めに断る
''', '''    row = _row(conn, note_id)
    _check_modify(user, row, note_id, "更新")
'''),
       (S, '''        row = _row(conn, note_id)
        _check_modify(user, row, note_id, "更新")
''', '''        _check_modify(user, _row(conn, note_id), note_id, "更新")
''')], CL.RESIDUAL_REPROS["RES-STALE-FIELDS"]),
    from_case("TD-04", "AUTH-04"),
    from_case("TD-05", "AUTH-05"),
    from_case("TD-06", "EXC-01"),
    from_case("TD-07", "BND-04"),
    from_case("TD-08", "RACE-02"),
    from_case("TD-09", "TX-02"),
    from_case("TD-10", "SQL-02"),
    from_case("TD-11", "SEC-02"),
    from_case("TD-12", "VAL-02"),
]
