import logging

# テスト中は警告ログを標準エラーに流さない（内容の検査は assertLogs で行う）
logging.getLogger("notes").addHandler(logging.NullHandler())
