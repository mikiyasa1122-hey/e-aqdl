# インターフェースの契約（notes パッケージ）

仕様は `docs/spec.md`。ここには、呼び出し側（と受け入れテスト）が使う名前・引数・戻り値・差し込み口を決める。
`notes/errors.py`（例外）は用意済みで、変えない。`notes/db.py`（接続・スキーマ・利用者の追加）も原則変えないが、
アーカイブ機能（R11〜R16）に限り、notes テーブルの列 `archived` の追加と、`db.connect` による旧スキーマ DB の移行だけ変えてよい（それ以外は変えない）。
作るのは `notes/service.py`・`notes/ratelimit.py`・`notes/share.py` とテスト。

## 利用者
`db.add_user(conn, user_id, org_id, name, role="member"|"admin", api_key=...)` が返す dict（id・org_id・name・role・api_key）。
このライブラリは、渡された dict の role・org_id を信用する。

## notes/db.py
- 新規 DB の notes に `archived INTEGER NOT NULL DEFAULT 0`（0=通常、1=アーカイブ済み）。
- `connect` は archived 列のない旧スキーマの DB を開いたとき列を追加する（既存データは archived=0。繰り返し開いてもエラーにならない）。

## notes/service.py
- `hooks`: `before_write(step)` メソッドを持つオブジェクト。既定は何もしない。テストは `service.hooks.before_write = 関数` で差し替え、
  並行の操作や途中の失敗を再現する。次の時点で、次の step の文字列で呼ぶ:
  - `create_note`: メモの行を挿入した後、タグを挿入する前（同じトランザクションの中）: `"create:tags"`
  - `get_note`: 見られるかを確かめた後、閲覧数を増やすトランザクションを始める前: `"view"`
  - `update_note`: 呼び出しの最初の確認の後、書き込みのトランザクションを始める前: `"update"`
  - `delete_note`: タグを消した後、メモを消す前（同じトランザクションの中）: `"delete:note"`
  - `transfer_notes`: 各メモを移す直前（同じトランザクションの中）: `"transfer:<メモの id>"`
  - `archive_note`／`unarchive_note`: 呼び出しの最初の確認の後、書き込みのトランザクションを始める前: `"archive"`／`"unarchive"`。
    状態を更新した後、監査ログを書く前（同じトランザクションの中）: `"archive:audit"`／`"unarchive:audit"`
- `create_note(conn, user, title, body, tags=(), private=False) -> int`（メモの id）。監査ログに action `"create"`・detail `"note=<id>"`。
- `get_note(conn, user, note_id) -> dict`: notes の行のすべての列（id・org_id・owner_id・title・body・private・version・views・updated_at）と
  `archived`（0 か 1）・`tags`（タグの列。名前の順）。閲覧のたびに views を1増やし、増やした後の値を返す。
- `archive_note(conn, user, note_id) -> int`・`unarchive_note(conn, user, note_id) -> int`（新しい version。updated_at は変えない）。
  監査ログに `"archive"`／`"unarchive"`・`"note=<id>"`。すでにその状態なら Conflict。
- `list_notes(conn, user, page=1, per_page=20, sort="updated", include_archived=False) -> dict`:
  `{"items": [{"id", "title", "updated_at"}, ...], "total": 件数, "page": page, "per_page": per_page}`。
  既定ではアーカイブ済みを含めない（total も同じ）。include_archived は真偽値だけ（それ以外は ValidationError）。
  sort は `"updated"`（updated_at の新しい順、同じなら id の大きい順）か `"title"`（題名順、同じなら id の小さい順）。
- `search_notes(conn, user, q, tags=(), include_archived=False) -> list`: `[{"id", "title"}, ...]`（id の順）。題名か本文の部分一致。
- `update_note(conn, user, note_id, version, title=None, body=None, tags=None) -> int`（新しい version）。
  アーカイブ済みのメモは Conflict。None の項目は変えない。tags を渡したら置き換える。監査ログに `"update"`・`"note=<id>"`。
- `delete_note(conn, user, note_id) -> None`。監査ログに `"delete"`・`"note=<id>"`。
- `transfer_notes(conn, admin, from_user, to_user) -> int`（移した件数）。移したメモの version を1増やす。
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
