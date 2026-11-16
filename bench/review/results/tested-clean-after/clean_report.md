# クリーンケース群の結果（EVAL-01）

| 指標 | 値 |
|---|---|
| 回数（判定済み） | 30（未判定 0・無効な実行 0） |
| クリーンケース正答率（合格） | 0.900（27/30） |
| 不要なブロック率 | 0.000（0/30） |
| 要再調査（重大の指摘が実在と判定された「要修正」） | 3 |
| 重大の指摘 | 3 |
| 重大の誤検知 | 0（{}） |
| 軽微の指摘（うち改善提案） | 28（28） |
| 報告の形式の不正（エンジンが受け付けない） | 0 |
| 判定を読み取れない | 0 |
| 1回の費用（USD） | 0.153 ± 0.030 |
| 1回の時間（秒） | 32.350 ± 3.700 |

## ケースごと

| ケース | 回 | 判定 | 重大（分類） | 軽微 | 形式 |
|---|---|---|---|---|---|
| CLN-01 | 1 | 合格 | - | 1 | OK |
| CLN-01 | 2 | 要修正 | 重大1:fix_required:real_other | 1 | OK |
| CLN-01 | 3 | 合格 | - | 1 | OK |
| CLN-02 | 1 | 合格 | - | 1 | OK |
| CLN-02 | 2 | 合格 | - | 1 | OK |
| CLN-02 | 3 | 合格 | - | 1 | OK |
| CLN-03 | 1 | 合格 | - | 1 | OK |
| CLN-03 | 2 | 合格 | - | 0 | OK |
| CLN-03 | 3 | 合格 | - | 1 | OK |
| CLN-04 | 1 | 合格 | - | 1 | OK |
| CLN-04 | 2 | 要修正 | 重大1:fix_required:real_other | 1 | OK |
| CLN-04 | 3 | 合格 | - | 1 | OK |
| CLN-05 | 1 | 合格 | - | 1 | OK |
| CLN-05 | 2 | 合格 | - | 1 | OK |
| CLN-05 | 3 | 要修正 | 重大1:fix_required:real_other | 0 | OK |
| CLN-06 | 1 | 合格 | - | 1 | OK |
| CLN-06 | 2 | 合格 | - | 1 | OK |
| CLN-06 | 3 | 合格 | - | 1 | OK |
| CLN-07 | 1 | 合格 | - | 1 | OK |
| CLN-07 | 2 | 合格 | - | 1 | OK |
| CLN-07 | 3 | 合格 | - | 1 | OK |
| CLN-08 | 1 | 合格 | - | 1 | OK |
| CLN-08 | 2 | 合格 | - | 1 | OK |
| CLN-08 | 3 | 合格 | - | 1 | OK |
| CLN-09 | 1 | 合格 | - | 1 | OK |
| CLN-09 | 2 | 合格 | - | 1 | OK |
| CLN-09 | 3 | 合格 | - | 1 | OK |
| CLN-10 | 1 | 合格 | - | 1 | OK |
| CLN-10 | 2 | 合格 | - | 1 | OK |
| CLN-10 | 3 | 合格 | - | 1 | OK |

## 重大の指摘の一覧（判定役の理由）

- CLN-01#2 [重大1] fix_required / real_other: 区分: 実装 / 対象: R3, AC3, notes/service.py:279（list_notes） / 対応区分: 修正必須 / 内容: `sort` に list や dict などハッシュできない値を渡すと、`sort not in SORT_COLUMNS` の dict 検索で ValidationError ではなく TypeError が出る。 / 成立条件: `list_n  
  判定役: sort にハッシュ不能な値（[] や {}）を渡すと ValidationError ではなく TypeError になり、R3 の範囲外は ValidationError という規定に反する。
- CLN-04#2 [重大1] fix_required / real_other: 区分: 実装 / 対象: R3, AC3, notes/service.py:140 `list_notes` / 対応区分: 修正必須 / 内容: `sort` に list・dict など hashable でない値を渡すと、`sort not in SORT_COLUMNS`（dict への所属判定）が TypeError を出す。仕様は「sort は updated か title だけ。  
  判定役: sort に list などの hashable でない値を渡すと `in SORT_COLUMNS` が TypeError になり、仕様の「範囲外は ValidationError」に反するため実在する欠陥。
- CLN-05#3 [重大1] fix_required / real_other: 区分: 実装 / 対象: R3, AC3, notes/service.py:139 の list_notes / 対応区分: 修正必須 / 内容: sort に updated・title 以外の値を渡すと ValidationError にするべきところ、辞書のキーにできない値（list・dict など）では `sort not in SORT_COLUMNS` が TypeError を投げ  
  判定役: list_notes(sort=[]) のようにハッシュできない sort を渡すと `sort not in SORT_COLUMNS` が TypeError を投げ、範囲外は ValidationError とする R3 に反するため、実在する欠陥である。
