# PERF-01 の結果

| 課題 | 条件 | 回 | 隠しテストの合格率 | 失敗（必須 / 参考） | 失敗した repro | 自作テスト | 費用 | 時間 | 実行の回数 | 人の返答 | E-AQDL の状態 | レビュー | 修正の回数 | 終わり方 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| archive | eaqdl | 1 | 1.000 (53/53) | 0 / 0 | - | 118 | $1.7109 | 8.0 分 | 2 | 1 | closed | 1 | 0 | done |
| archive | eaqdl | 2 | 1.000 (53/53) | 0 / 0 | - | 94 | $1.4262 | 6.6 分 | 3 | 2 | closed | 1 | 0 | done |
| archive | eaqdl | 3 | 1.000 (53/53) | 0 / 0 | - | 96 | $1.2387 | 6.0 分 | 2 | 1 | closed | 1 | 0 | done |
| archive | plain | 1 | 1.000 (53/53) | 0 / 0 | - | 70 | $0.1625 | 0.4 分 | 1 | 0 | - | - | - | done |
| archive | plain | 2 | 1.000 (53/53) | 0 / 0 | - | 68 | $0.1543 | 0.4 分 | 1 | 0 | - | - | - | done |
| archive | plain | 3 | 1.000 (53/53) | 0 / 0 | - | 69 | $0.1667 | 0.4 分 | 1 | 0 | - | - | - | done |
| spec | eaqdl | 1 | 0.941 (95/101) | 2 / 4 | res_authz_toctou, res_page_overflow, res_stale_fields | 86 | $1.6828 | 8.6 分 | 2 | 1 | closed | 1 | 0 | done |
| spec | eaqdl | 2 | 0.960 (97/101) | 2 / 2 | res_authz_toctou, res_stale_fields | 102 | $1.8181 | 9.8 分 | 2 | 1 | closed | 1 | 0 | done |
| spec | eaqdl | 3 | 0.941 (95/101) | 2 / 4 | res_authz_toctou, res_page_overflow, res_stale_fields | 67 | $1.3597 | 6.9 分 | 2 | 1 | closed | 1 | 0 | done |
| spec | plain | 1 | 0.941 (95/101) | 2 / 4 | res_authz_toctou, res_page_overflow, res_stale_fields | 31 | $0.4504 | 1.8 分 | 1 | 0 | - | - | - | done |
| spec | plain | 2 | 0.941 (95/101) | 2 / 4 | res_authz_toctou, res_page_overflow, res_stale_fields | 42 | $0.3685 | 1.9 分 | 1 | 0 | - | - | - | done |
| spec | plain | 3 | 0.941 (95/101) | 2 / 4 | res_authz_toctou, res_page_overflow, res_stale_fields | 45 | $0.3799 | 2.0 分 | 1 | 0 | - | - | - | done |
| vague | eaqdl | 1 | 0.604 (61/101) | 38 / 2 | auth_04, bnd_01, res_page_overflow, res_tag_newline, sql_04, val_02, val_03, val_04 | 94 | $2.146 | 11.6 分 | 2 | 1 | closed | 1 | 0 | done |
| vague | eaqdl | 2 | 0.564 (57/101) | 41 / 3 | auth_04, bnd_01, res_authz_toctou, res_tag_newline, sql_04, tx_02, val_02, val_03, val_04 | 88 | $2.3184 | 13.0 分 | 2 | 1 | closed | 1 | 0 | done |
| vague | eaqdl | 3 | 0.634 (64/101) | 35 / 2 | bnd_01, res_authz_toctou, res_stale_fields, res_tag_newline, sql_04, val_02, val_03, val_04 | 91 | $2.1869 | 11.4 分 | 2 | 1 | closed | 1 | 0 | done |
| vague | plain | 1 | 0.535 (54/101) | 43 / 4 | auth_04, bnd_01, bnd_04, res_page_overflow, res_rollback_mask, res_tag_newline, sql_04, val_02, val_03, val_04 | 19 | $0.29 | 1.4 分 | 1 | 0 | - | - | - | done |
| vague | plain | 2 | 0.535 (54/101) | 43 / 4 | auth_04, bnd_01, bnd_04, res_page_overflow, res_rollback_mask, res_tag_newline, sql_04, val_02, val_03, val_04 | 21 | $0.2875 | 1.6 分 | 1 | 0 | - | - | - | done |
| vague | plain | 3 | 0.535 (54/101) | 43 / 4 | auth_04, bnd_01, bnd_04, res_page_overflow, res_rollback_mask, res_tag_newline, sql_04, val_02, val_03, val_04 | 20 | $0.2909 | 1.5 分 | 1 | 0 | - | - | - | done |

## 課題 × 条件の集計（平均・括弧内は最小〜最大）

| 課題 | 条件 | 回数 | 必須の失敗 | 参考の失敗 | 費用（USD） | 時間（分） | 人の返答 | 完了まで進んだ |
|---|---|---|---|---|---|---|---|---|
| archive | eaqdl | 3 | 0.00（0〜0） | 0.00（0〜0） | 1.46（1.2387〜1.7109） | 6.87（6〜8） | 1.33（1〜2） | 3/3 |
| archive | plain | 3 | 0.00（0〜0） | 0.00（0〜0） | 0.16（0.1543〜0.1667） | 0.40（0.4〜0.4） | 0.00（0〜0） | 3/3 |
| spec | eaqdl | 3 | 2.00（2〜2） | 3.33（2〜4） | 1.62（1.3597〜1.8181） | 8.43（6.9〜9.8） | 1.00（1〜1） | 3/3 |
| spec | plain | 3 | 2.00（2〜2） | 4.00（4〜4） | 0.40（0.3685〜0.4504） | 1.90（1.8〜2） | 0.00（0〜0） | 3/3 |
| vague | eaqdl | 3 | 38.00（35〜41） | 2.33（2〜3） | 2.22（2.146〜2.3184） | 12.00（11.4〜13） | 1.00（1〜1） | 3/3 |
| vague | plain | 3 | 43.00（43〜43） | 4.00（4〜4） | 0.29（0.2875〜0.2909） | 1.50（1.4〜1.6） | 0.00（0〜0） | 3/3 |

モデル: claude-sonnet-5-5
