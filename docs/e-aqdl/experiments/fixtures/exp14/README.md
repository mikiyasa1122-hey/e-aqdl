# inventory

社内の在庫 API のクライアント。

- `INVENTORY_URL`: 在庫 API の URL（既定は https://inventory.internal.example.com/api 。社内ネットワークからのみ到達できる）
- テスト: `python3 -m unittest discover -s tests -t .`
  - `tests/test_live.py` は本物の在庫 API に接続する結合テスト
- Lint: `ruff check`（設定は pyproject.toml）
