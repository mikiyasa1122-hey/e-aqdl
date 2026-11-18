# インターフェースの契約（notes パッケージ）

仕様は `docs/spec.md`。ここには、呼び出し側（と受け入れテスト）が使う名前・引数・戻り値・差し込み口を決める。
`notes/db.py`（接続・スキーマ・利用者の追加）は、R14 で `notes` テーブルに `archived` 列を足す範囲に限って変える。`notes/errors.py`（例外）は変えない。
作るのは `notes/service.py`（`archive_note`・`unarchive_note` を含む）・`notes/ratelimit.py`・`notes/share.py` とテスト。

## 利用者
`db.add_user(conn, user_id, org_id, name, role="member"|"admin", api_key=...)` が返す dict（id・org_id・name・role・api_key）。
このライブラリは、渡された dict の role・org_id を信用する。

## notes/db.py
- `connect(path=":memory:")`: `notes` に `archived INTEGER NOT NULL DEFAULT 0` がなければ `ALTER TABLE` で足す（冪等。既存行は archived=0。別の接続が同時に足した場合は成功とみなす）。

## notes/service.py
- `hooks`: `before_write(step)` メソッドを持つオブジェクト。既定は何もしない。テストは `service.hooks.before_write = 関数` で差し替え、
  並行の操作や途中の失敗を再現する。次の時点で、次の step の文字列で呼ぶ:
  - `create_note`: メモの行を挿入した後、タグを挿入する前（同じトランザクションの中）: `"create:tags"`
  - `get_note`: 見られるかを確かめた後、閲覧数を増やすトランザクションを始める前: `"view"`
  - `update_note`: 呼び出しの最初の確認の後、書き込みのトランザクションを始める前: `"update"`
  - `delete_note`: タグを消した後、メモを消す前（同じトランザクションの中）: `"delete:note"`
  - `transfer_notes`: 各メモを移す直前（同じトランザクションの中）: `"transfer:<メモの id>"`
  - `archive_note`: 呼び出しの最初の確認の後、書き込みのトランザクションを始める前: `"archive"`
  - `unarchive_note`: 同様に: `"unarchive"`
  - `archive_note`・`unarchive_note`: 同じトランザクションの中、UPDATE の後・監査ログの直前: `"archive:audit"`・`"unarchive:audit"`
- `create_note(conn, user, title, body, tags=(), private=False) -> int`（メモの id）。監査ログに action `"create"`・detail `"note=<id>"`。
- `get_note(conn, user, note_id) -> dict`: notes の行のすべての列（id・org_id・owner_id・title・body・private・version・views・updated_at・archived（0 か 1））と
  `tags`（タグの列。名前の順）。閲覧のたびに views を1増やし、増やした後の値を返す。
- `list_notes(conn, user, page=1, per_page=20, sort="updated", include_archived=False) -> dict`:
  `{"items": [{"id", "title", "updated_at"}, ...], "total": 件数, "page": page, "per_page": per_page}`。
  `include_archived` は bool のみ（それ以外は ValidationError）。False なら archived=1 を items・total から除き、True なら含める。他人の非公開・他組織は常に含めない。
  sort は `"updated"`（updated_at の新しい順、同じなら id の大きい順）か `"title"`（題名順、同じなら id の小さい順）。
- `search_notes(conn, user, q, tags=(), include_archived=False) -> list`: `[{"id", "title"}, ...]`（id の順）。題名か本文の部分一致。
  `include_archived` は list_notes と同じ（既定では archived=1 を除く）。
- `update_note(conn, user, note_id, version, title=None, body=None, tags=None) -> int`（新しい version）。
  None の項目は変えない。tags を渡したら置き換える。監査ログに `"update"`・`"note=<id>"`。アーカイブ済みのメモは（version が古くても最新でも）Conflict。
- `archive_note(conn, user, note_id) -> int`・`unarchive_note(conn, user, note_id) -> int`（新しい version）。権限は更新と同じ
  （見えない→NotFound、権限なし→Forbidden、すでにその状態→Conflict）。version を1増やし、updated_at は変えない。
  監査ログに `"archive"`／`"unarchive"`・`"note=<id>"`（同じトランザクション）。
- `delete_note(conn, user, note_id) -> None`。監査ログに `"delete"`・`"note=<id>"`。アーカイブ済みも削除できる。
- `transfer_notes(conn, admin, from_user, to_user) -> int`（移した件数）。移したメモの version を1増やす。アーカイブ済みも移し（件数に数える）、アーカイブ状態は保つ。
  監査ログに `"transfer"`・`"from=<元の id> to=<先の id> count=<件数>"`。
- `export_audit(conn, admin) -> list`: `[{"id", "action", "detail", "actor"（利用者の name）, "actor_key"（伏せた api_key）}, ...]`（id の順）。
- ログは logger `"notes"` に INFO で、作成したメモの id と作成者の id を出す（本文は出さない）。

## notes/ratelimit.py
- `RateLimiter(limit=10, window_sec=60, clock=time.monotonic)`。`hit(user_id) -> int`（この枠の残りの回数）。超えたら `RateLimited`。
- `hook`: 引数なしの関数（既定は何もしない）。`hit` の中で、枠を判定した後・回数を数える前に呼ぶ（テストが同時の呼び出しを再現する）。

## notes/share.py
- `make_link(secret, note_id, ttl_sec=3600, now=None) -> str`（`"<note_id>.<期限>.<署名>"`。期限は `now` 秒 + ttl_sec。署名は
  `"<note_id>:<期限>"` の HMAC-SHA256 の16進の小文字）。
- `verify_link(secret, link, now=None) -> int`（note_id）。無効なら `Forbidden`。
- ログは logger `"notes.share"` に出してよい（署名は出さない）。
