import os
import sqlite3
import tempfile
import unittest


class UsersTest(unittest.TestCase):
    def test_list_users(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "t.db")
        sqlite3.connect(db).executescript(open("migrations/001_create_users.sql").read() +
                                          "INSERT INTO users VALUES (1,'a','a@example.com');")
        os.environ["DATABASE_URL"] = "sqlite:///" + db
        os.environ["ENV_FILE"] = "/nonexistent"
        from app.users import list_users
        self.assertEqual(list_users(), [{"id": 1, "name": "a", "email": "a@example.com"}])
