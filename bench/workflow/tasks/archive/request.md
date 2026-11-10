# 追加の依頼: メモのアーカイブ

既存の notes パッケージ（仕様は docs/spec.md、契約は docs/contract.md）に、メモのアーカイブ機能を足してください。
既存の振る舞いとテストは保ってください。テストも足してください（`python3 -m unittest discover -s tests -t .`）。

## R11 メモをアーカイブ・復元できる
- `service.archive_note(conn, user, note_id) -> int` と `service.unarchive_note(conn, user, note_id) -> int`（新しい version を返す）。
- できる人は、更新（R4）と同じ: 作成者と同じ組織の管理者。非公開のメモは作成者だけ。見られないメモは NotFound、見られるが権限がなければ Forbidden。
- アーカイブすると version を1増やす（復元も同じ）。すでにその状態（アーカイブ済みをアーカイブ、未アーカイブを復元）なら Conflict。
- 監査ログに action `"archive"`／`"unarchive"`、detail `"note=<id>"` を、同じトランザクションで記録する。失敗したら何も残さない。
- アーカイブ済みのメモは、`list_notes` と `search_notes` の結果に既定では含めない（`total` も含めない数にする）。
  どちらにも引数 `include_archived=False` を足し、True なら含める。
- `get_note` はアーカイブ済みでも読める。返す dict に `archived`（0 か 1）を含める。
- アーカイブ済みのメモは更新できない（`update_note` は Conflict）。削除と移管はできる。
- 保存は notes テーブルの列 `archived INTEGER NOT NULL DEFAULT 0`（`notes/db.py` のスキーマを変えてよい）。
