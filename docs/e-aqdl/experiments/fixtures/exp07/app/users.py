from .db import connect


def list_users():
    with connect() as c:
        return [dict(zip(("id", "name", "email"), r)) for r in c.execute("SELECT id, name, email FROM users ORDER BY id")]
