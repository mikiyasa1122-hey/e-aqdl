# PERF-01 パイロットの結果

| 条件 | 回 | 隠しテストの合格率 | 失敗した repro | 自作テスト | 費用 | 時間 | 実行の回数 | 人の返答 | E-AQDL の状態 | レビュー | 修正の回数 | 終わり方 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| eaqdl | 1 | 0.941 (95/101) | res_authz_toctou, res_page_overflow, res_stale_fields | 82 | $1.7926 | 8.6 分 | 2 | 1 | closed | 1 | 0 | done |
| plain | 1 | 0.941 (95/101) | res_authz_toctou, res_page_overflow, res_stale_fields | 29 | $0.3102 | 1.5 分 | 1 | 0 | - | - | - | done |
