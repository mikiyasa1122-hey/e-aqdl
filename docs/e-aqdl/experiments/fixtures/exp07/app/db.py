"""DB 接続。DATABASE_URL（sqlite:///パス）を使う。"""
import os
import sqlite3


def load_env(path=".env"):
    """簡易的な .env 読み込み（既に環境変数があればそちらを優先）。"""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def connect():
    load_env(os.environ.get("ENV_FILE", ".env"))
    url = os.environ["DATABASE_URL"]
    assert url.startswith("sqlite:///")
    return sqlite3.connect(url[len("sqlite:///"):])
