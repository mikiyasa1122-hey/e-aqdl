チーム用のメモを保存する HTTP API を作ってください。

## 制約
- Python 3.8 以上、標準ライブラリだけで作る（外部パッケージは使わない）
- 起動方法: `python3 server.py <ポート番号>`（このフォルダ直下の server.py）
- データはメモリ上に持てばよい（再起動で消えてよい）

## 利用者（固定）
リクエストの `Authorization: Bearer <トークン>` で利用者を判定する。

| トークン | 利用者 | 役割 |
|---|---|---|
| token-alice | alice | member |
| token-bob | bob | member |
| token-admin | admin | admin |

トークンがない・不正なら、どの API も 401 `{"error": "unauthorized"}`。

## メモ
`{"id": 整数, "title": 文字列, "body": 文字列, "owner": 利用者名}`。id は 1 から順に振る。

## API（レスポンスはすべて JSON。エラーは `{"error": "<コード>"}`）
1. `POST /notes` … 本文 `{"title": ..., "body": ...}` でメモを作る → 201 とメモ
   - title は文字列で、前後の空白を除いて 1〜100 文字。満たさなければ 400 `invalid_title`
   - body は文字列で 1000 文字以内（省略時は空文字）。満たさなければ 400 `invalid_body`
   - JSON として読めなければ 400 `invalid_json`
   - ヘッダー `Idempotency-Key` があるとき、同じ利用者が同じキーで再送したら新しく作らず、最初に作ったメモを 200 で返す。同じキーで内容（title・body）が違えば 409 `idempotency_conflict`。キーは利用者ごとに別に扱う
2. `GET /notes?limit=&offset=` … 見えるメモの一覧 → 200 `{"items": [...], "total": 件数}`（id の昇順）
   - member は自分のメモだけ、admin はすべてのメモが見える
   - limit は 1〜50（省略時 20）、offset は 0 以上（省略時 0）。範囲外・整数でない値は 400 `invalid_paging`
3. `GET /notes/<id>` … 1件 → 200
4. `PUT /notes/<id>` … 本文 `{"title": ..., "body": ...}` で更新 → 200 と更新後のメモ（検証は POST と同じ）
5. `DELETE /notes/<id>` … 削除 → 204（本文なし）

## 権限
- 自分のメモは、見る・更新・削除ができる
- admin は、他人のメモを見る・削除はできるが、更新はできない（403 `forbidden`）
- member が他人のメモを見る・更新・削除しようとしたら、存在を隠すため 404 `not_found`（存在しない id と同じ扱い）
- 存在しない id・定義されていないパスは 404 `not_found`

完成したら、どう確かめたかを短く報告してください。
