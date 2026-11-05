"""在庫の日次レポート（古いコード。テストはまだない）。"""
import os
import sys
import json
from datetime import datetime


def summarize(items, warn_below=5):
    lines = []
    total = 0
    unused = datetime.now()
    for it in items:
        try:
            total += it["stock"]
        except:
            continue
        if it.get("note") == None:
            pass
        if it["stock"] < warn_below:
            lines.append("在庫僅少: %s (%d)" % (it["name"], it["stock"]))
    lines.append("合計: %d" % total)
    return "\n".join(lines)


def print_report(items, out=sys.stdout):
    out.write(summarize(items) + "\n")
