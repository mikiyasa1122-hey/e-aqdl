チームの TODO を管理する HTTP API を作ってください（条件④の Memory を作るための事前の run。本番のお題とは別の題材）。

## 制約
- Python 3.8 以上、標準ライブラリだけ。起動は `python3 server.py <ポート番号>`。データはメモリ上でよい

## 利用者（固定）
`Authorization: Bearer <トークン>`。token-carol（carol, member）、token-dave（dave, member）、token-boss（boss, manager）。不正・なしは 401 `{"error": "unauthorized"}`

## TODO
`{"id": 整数, "text": 文字列, "done": 真偽, "assignee": 利用者名}`

## API
- `POST /todos` 本文 `{"text": ..., "assignee": ...}` → 201。text は前後の空白を除いて 1〜200 文字（400 `invalid_text`）。assignee は存在する利用者（400 `invalid_assignee`）。member は自分にしか割り当てられない（403 `forbidden`）、manager は誰にでも割り当てられる
- `GET /todos?done=true|false` → 200 `{"items": [...]}`。member は自分の担当だけ、manager はすべて
- `POST /todos/<id>/complete` → 200。担当者だけが完了にできる（manager も他人のものは不可: 403 `forbidden`）。同じ TODO を2回完了にしても結果は同じ（200、状態は変わらない）
- 見えない・存在しない id は 404 `not_found`

完成したら、どう確かめたかを短く報告してください。
