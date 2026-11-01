"""migrations/ の SQL を、まだ適用していないものだけ順に適用する。DATABASE_URL の DB に対して実行する。"""
import glob
import os

from app.db import connect

with connect() as c:
    c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    done = {r[0] for r in c.execute("SELECT name FROM schema_migrations")}
    for path in sorted(glob.glob("migrations/*.sql")):
        name = os.path.basename(path)
        if name in done:
            continue
        c.executescript(open(path, encoding="utf-8").read())
        c.execute("INSERT INTO schema_migrations VALUES (?)", (name,))
        print("applied", name)
