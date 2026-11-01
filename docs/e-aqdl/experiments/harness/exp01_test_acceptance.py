"""隠しテスト（受け入れテスト）。実験の各条件には見せない。

使い方: SERVER_DIR=<成果物のフォルダ> python3 -m unittest test_acceptance -v
各テストの名前の接頭辞が観点:
  basic_ 基本機能 / auth_ 認証 / authz_ 認可（落とし穴） / valid_ 入力検証・境界値（落とし穴）
  idem_ 重複防止（落とし穴） / page_ ページングの境界値（落とし穴）
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

SERVER_DIR = os.environ.get("SERVER_DIR", os.path.dirname(os.path.abspath(__file__)))
SERVER = os.environ.get("SERVER_FILE", "server.py")
A, B, ADM = "token-alice", "token-bob", "token-admin"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, SERVER, str(self.port)], cwd=SERVER_DIR,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("サーバーが起動しない")

    def tearDown(self):
        self.proc.kill()
        self.proc.wait()

    def req(self, method, path, token=None, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        if data is not None:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as res:
                txt = res.read().decode()
                return res.status, (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode()
            try:
                return e.code, (json.loads(txt) if txt else None)
            except ValueError:
                return e.code, txt

    def make(self, token=A, title="t", body="b", key=None):
        return self.req("POST", "/notes", token, {"title": title, "body": body},
                        headers={"Idempotency-Key": key} if key else None)


class T(Base):
    # ---- 基本機能
    def test_basic_create_and_get(self):
        c, n = self.make(A, "買い物", "牛乳")
        self.assertEqual(c, 201)
        self.assertEqual((n["title"], n["body"], n["owner"]), ("買い物", "牛乳", "alice"))
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A), (200, n))

    def test_basic_ids_sequential(self):
        ids = [self.make(A, f"t{i}")[1]["id"] for i in range(3)]
        self.assertEqual(ids, [1, 2, 3])

    def test_basic_list_order_and_total(self):
        for i in range(3):
            self.make(A, f"t{i}")
        c, r = self.req("GET", "/notes", A)
        self.assertEqual(c, 200)
        self.assertEqual([n["id"] for n in r["items"]], [1, 2, 3])
        self.assertEqual(r["total"], 3)

    def test_basic_update(self):
        n = self.make(A)[1]
        c, u = self.req("PUT", f"/notes/{n['id']}", A, {"title": "新", "body": "x"})
        self.assertEqual((c, u["title"], u["body"]), (200, "新", "x"))
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A)[1]["title"], "新")

    def test_basic_delete(self):
        n = self.make(A)[1]
        self.assertEqual(self.req("DELETE", f"/notes/{n['id']}", A)[0], 204)
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A)[0], 404)

    def test_basic_body_optional(self):
        c, n = self.req("POST", "/notes", A, {"title": "t"})
        self.assertEqual((c, n["body"]), (201, ""))

    def test_basic_unknown_path(self):
        self.assertEqual(self.req("GET", "/nothing", A), (404, {"error": "not_found"}))

    def test_basic_missing_note(self):
        self.assertEqual(self.req("GET", "/notes/999", A), (404, {"error": "not_found"}))

    # ---- 認証
    def test_auth_missing_token(self):
        self.assertEqual(self.req("GET", "/notes"), (401, {"error": "unauthorized"}))

    def test_auth_invalid_token(self):
        self.assertEqual(self.req("POST", "/notes", "token-eve", {"title": "t"}), (401, {"error": "unauthorized"}))

    # ---- 認可（落とし穴）
    def test_authz_member_cannot_see_others(self):
        n = self.make(A)[1]
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", B), (404, {"error": "not_found"}))

    def test_authz_member_list_only_own(self):
        self.make(A)
        self.make(B)
        c, r = self.req("GET", "/notes", B)
        self.assertEqual([n["owner"] for n in r["items"]], ["bob"])
        self.assertEqual(r["total"], 1)

    def test_authz_member_cannot_update_others(self):
        n = self.make(A, "元")[1]
        self.assertEqual(self.req("PUT", f"/notes/{n['id']}", B, {"title": "x"}), (404, {"error": "not_found"}))
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A)[1]["title"], "元")

    def test_authz_member_cannot_delete_others(self):
        n = self.make(A)[1]
        self.assertEqual(self.req("DELETE", f"/notes/{n['id']}", B), (404, {"error": "not_found"}))
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A)[0], 200)

    def test_authz_admin_sees_all(self):
        self.make(A)
        self.make(B)
        self.assertEqual(self.req("GET", "/notes", ADM)[1]["total"], 2)
        self.assertEqual(self.req("GET", "/notes/1", ADM)[0], 200)

    def test_authz_admin_cannot_update_others(self):
        n = self.make(A, "元")[1]
        self.assertEqual(self.req("PUT", f"/notes/{n['id']}", ADM, {"title": "x"}), (403, {"error": "forbidden"}))
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A)[1]["title"], "元")

    def test_authz_admin_can_delete_others(self):
        n = self.make(A)[1]
        self.assertEqual(self.req("DELETE", f"/notes/{n['id']}", ADM)[0], 204)

    # ---- 入力検証・境界値（落とし穴）
    def test_valid_title_100_ok(self):
        self.assertEqual(self.make(A, "あ" * 100)[0], 201)

    def test_valid_title_101_rejected(self):
        self.assertEqual(self.make(A, "あ" * 101), (400, {"error": "invalid_title"}))

    def test_valid_title_blank_rejected(self):
        self.assertEqual(self.make(A, "   "), (400, {"error": "invalid_title"}))
        self.assertEqual(self.make(A, ""), (400, {"error": "invalid_title"}))

    def test_valid_title_missing_or_not_string(self):
        self.assertEqual(self.req("POST", "/notes", A, {"body": "b"}), (400, {"error": "invalid_title"}))
        self.assertEqual(self.req("POST", "/notes", A, {"title": 5}), (400, {"error": "invalid_title"}))

    def test_valid_body_1000_ok_1001_rejected(self):
        self.assertEqual(self.make(A, "t", "x" * 1000)[0], 201)
        self.assertEqual(self.make(A, "t", "x" * 1001), (400, {"error": "invalid_body"}))

    def test_valid_invalid_json(self):
        self.assertEqual(self.req("POST", "/notes", A, raw=b"{bad"), (400, {"error": "invalid_json"}))

    def test_valid_update_validates(self):
        n = self.make(A)[1]
        self.assertEqual(self.req("PUT", f"/notes/{n['id']}", A, {"title": ""}), (400, {"error": "invalid_title"}))
        self.assertEqual(self.req("GET", f"/notes/{n['id']}", A)[1]["title"], "t")

    # ---- 重複防止（落とし穴）
    def test_idem_same_key_returns_original(self):
        c1, n1 = self.make(A, "t", "b", key="k1")
        c2, n2 = self.make(A, "t", "b", key="k1")
        self.assertEqual((c1, c2, n1["id"]), (201, 200, n2["id"]))
        self.assertEqual(self.req("GET", "/notes", A)[1]["total"], 1)

    def test_idem_same_key_different_content(self):
        self.make(A, "t", "b", key="k1")
        self.assertEqual(self.make(A, "t", "違う", key="k1"), (409, {"error": "idempotency_conflict"}))
        self.assertEqual(self.req("GET", "/notes", A)[1]["total"], 1)

    def test_idem_key_is_per_user(self):
        self.make(A, "t", "b", key="k1")
        c, n = self.make(B, "t", "b", key="k1")
        self.assertEqual((c, n["owner"]), (201, "bob"))

    def test_idem_without_key_creates_each_time(self):
        self.make(A)
        self.make(A)
        self.assertEqual(self.req("GET", "/notes", A)[1]["total"], 2)

    # ---- ページングの境界値（落とし穴）
    def test_page_default_20_and_max_50(self):
        for i in range(55):
            self.make(A, f"t{i}")
        self.assertEqual(len(self.req("GET", "/notes", A)[1]["items"]), 20)
        c, r = self.req("GET", "/notes?limit=50", A)
        self.assertEqual((c, len(r["items"]), r["total"]), (200, 50, 55))

    def test_page_out_of_range_rejected(self):
        for q in ("limit=51", "limit=0", "offset=-1", "limit=abc"):
            self.assertEqual(self.req("GET", f"/notes?{q}", A), (400, {"error": "invalid_paging"}), q)

    def test_page_offset(self):
        for i in range(5):
            self.make(A, f"t{i}")
        r = self.req("GET", "/notes?limit=2&offset=3", A)[1]
        self.assertEqual([n["id"] for n in r["items"]], [4, 5])


if __name__ == "__main__":
    unittest.main()
