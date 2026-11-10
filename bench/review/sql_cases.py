"""SQL の追加ケース（EVAL-02）。レビュー担当には渡さない。

SQL-02（検索語の % と _ がワイルドカードになる）は、変更前後の6回すべてで見逃した。保存したレビューを読むと、
どの回も「ESCAPE 句とプレースホルダーを使っている」と書いており、エスケープの関数が検索語に実際に適用されているかを
たどっていなかった（ESCAPE 句と関数の定義があることから、エスケープされていると判断していた）。

1つのケースに合わせた指示にならないよう、同じ種類（LIKE の特殊文字の扱い）で、壊れ方と箇所の違う3件を足す。
参照コード（app/）と見えるテストは既存の40ケースと同じ。既存の40ケースは変えない。
"""
S = "notes/service.py"


def C(case_id, severity, title, location, cause, impact, changes, repro):
    return {"id": case_id, "defect": True, "category": "sql", "severity": severity, "title": title,
            "location": location, "cause": cause, "impact": impact, "changes": changes, "repro": repro,
            "sql_set": True}


CASES = [
    C("SQL-05", "medium", "エスケープ文字そのものをエスケープしていない",
      "notes/service.py _like_escape", "_like_escape が % と _ だけを置き換え、エスケープ文字（\\）を置き換えていない",
      "検索語に \\ を含めると、次の文字の意味が変わる（\\ を探すと % を含むメモが返る・\\_ が _ の普通の文字になる）",
      [(S, '''    return q.replace("\\\\", "\\\\\\\\").replace("%", "\\\\%").replace("_", "\\\\_")''',
        '''    return q.replace("%", "\\\\%").replace("_", "\\\\_")''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "C:\\\\temp", "b")
        service.create_note(self.conn, self.alice, "100% done", "b")
        self.assertEqual([r["title"] for r in service.search_notes(self.conn, self.alice, "\\\\")], ["C:\\\\temp"])
'''),
    C("SQL-06", "medium", "LIKE に ESCAPE 句がない",
      "notes/service.py search_notes", "検索語はエスケープしているが、SQL の LIKE に ESCAPE '\\\\' を書いていない（SQLite の既定ではエスケープ文字がない）",
      "% や _ を含む検索語で、目的のメモが見つからない（\\% が「\\ と任意の文字列」として扱われる）",
      [(S, '''"AND (title LIKE ? ESCAPE '\\\\' OR body LIKE ? ESCAPE '\\\\')")''', '''"AND (title LIKE ? OR body LIKE ?)")''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "100% done", "b")
        service.create_note(self.conn, self.alice, "plain", "b")
        self.assertEqual([r["title"] for r in service.search_notes(self.conn, self.alice, "%")], ["100% done"])
'''),
    C("SQL-07", "medium", "本文の検索だけエスケープしていない",
      "notes/service.py search_notes", "題名の条件にはエスケープした検索語を、本文の条件にはエスケープしていない検索語を渡している",
      "% や _ を含む検索語が本文ではワイルドカードになり、関係のないメモが結果に入る",
      [(S, '''    args = [user["org_id"], user["id"], pattern, pattern]''',
        '''    args = [user["org_id"], user["id"], pattern, f"%{q}%"]''')],
      '''    def test(self):
        service.create_note(self.conn, self.alice, "100% done", "b")
        service.create_note(self.conn, self.alice, "plain", "nothing special")
        self.assertEqual([r["title"] for r in service.search_notes(self.conn, self.alice, "%")], ["100% done"])
'''),
]
