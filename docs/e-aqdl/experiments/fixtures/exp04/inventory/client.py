"""在庫 API（社内ネットワークのみ）から商品の在庫を取得する。"""
import json
import os
import urllib.request

DEFAULT_URL = "https://inventory.internal.example.com/api"


def base_url():
    return os.environ.get("INVENTORY_URL", DEFAULT_URL)


def fetch_items(url=None, opener=urllib.request.urlopen, timeout=10):
    """在庫の一覧を返す。各要素は {"sku": str, "name": str, "stock": int}。"""
    with opener(f"{url or base_url()}/items", timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [{"sku": d["sku"], "name": d["name"], "stock": int(d["stock"])} for d in data["items"]]
