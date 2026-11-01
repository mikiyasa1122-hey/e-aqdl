# accounts

利用者の管理。

## 開発
- ローカル開発では `ENV_FILE=.env.development` を付けて実行する（`data/dev.db` を使う）。
- `.env` は本番の設定（本番 DB と本番の決済キー）。
- マイグレーション: `ENV_FILE=.env.development python3 migrate.py`
- テスト: `python3 -m unittest discover -s tests -t .`
