import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import flask  # noqa: F401
    from app import create_app
except ImportError:  # pragma: no cover
    flask = None


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class FlaskPageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-flask-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload_csv(self, content="单号,净重\nA,100\nB,90\nC,90\nD,80\n", day="2026-05-01"):
        data = {
            "date": day,
            "file": (io.BytesIO(content.encode("utf-8")), "tickets.csv"),
        }
        return self.client.post("/upload", data=data, content_type="multipart/form-data")

    def test_health_page_upload_query_and_tie_display(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        resp = self.upload_csv()
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/day?date=2026-05-01", resp.headers["Location"])

        resp = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("第 2-3 名", html)
        match = re.search(r'id="subtotal-access-count"[^>]*>\s*(\d+)\s*<', html)
        self.assertIsNotNone(match)
        accessed = int(match.group(1))
        self.assertLessEqual(accessed, 576)
        self.assertIn("576", html)
        self.assertIn("小计键访问数", html)

        # A balanced-tree worst interval plus weight zero reaches the proven
        # 540-key upper bound for two calibers.
        self.client.post(
            "/upload",
            data={"date": "2026-06-01", "csv_text": "X,0"},
        )
        worst = self.client.get(
            "/query?date=2026-06-01&ticket_no=X&start=2026-01-02&end=2026-12-30"
        ).get_data(as_text=True)
        worst_match = re.search(r'id="subtotal-access-count"[^>]*>\s*(\d+)\s*<', worst)
        self.assertEqual(int(worst_match.group(1)), 540)

        js = self.client.get(
            "/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_json()
        self.assertTrue(js["ok"])
        self.assertTrue(js["result"]["meter"]["within_limit"])
        self.assertLessEqual(js["result"]["meter"]["accessed"], 576)

    def test_hold_shows_held_note_and_blank_rank_then_release(self):
        self.upload_csv()
        resp = self.client.post(
            "/hold", data={"date": "2026-05-01", "ticket_no": "B", "reason": "tie check"}
        )
        self.assertEqual(resp.status_code, 303)

        html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertIn("目标单已暂扣", html)
        self.assertIn("留空", html)
        # All-caliber rank remains visible.
        self.assertIn("第 2-3 名", html)

        resp = self.client.post("/release", data={"date": "2026-05-01", "ticket_no": "B"})
        self.assertEqual(resp.status_code, 303)
        html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertNotIn("目标单已暂扣；未暂扣口径只显示总数", html)

    def test_reupload_preserves_hold(self):
        self.upload_csv()
        self.client.post("/hold", data={"date": "2026-05-01", "ticket_no": "A"})
        # Replacement CSV omits A.
        self.upload_csv("单号,净重\nB,95\nC,90\nD,80\n", "2026-05-01")
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn("原单暂缺但仍有效的暂扣记录", day_html)
        self.assertIn(">A<", day_html)

    def test_invalid_upload_does_not_replace_day(self):
        self.upload_csv()
        bad = "单号,净重\nX,10\nX,11\nY,70000\n"
        resp = self.upload_csv(bad)
        # HTML error path redirects back and flashes; follow it.
        self.assertEqual(resp.status_code, 303)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)
        self.assertIn(">B<", day_html)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class EncodingChoicePageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-flask-enc-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload_bytes(self, raw, day="2026-05-01", name="tickets.csv"):
        return self.client.post(
            "/upload",
            data={"date": day, "file": (io.BytesIO(raw), name)},
            content_type="multipart/form-data",
        )

    def ambiguous_bytes(self):
        # "茅,100" in GB18030 is also valid UTF-8 as "é,100".
        return "茅,100\nA,90\n".encode("gb18030")

    def test_bom_upload_uses_utf8(self):
        raw = b"\xef\xbb\xbf" + "单号,净重\nA,100\n".encode("utf-8")
        resp = self.upload_bytes(raw)
        self.assertEqual(resp.status_code, 303)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)

    def test_gb18030_only_upload_replaces_directly(self):
        resp = self.upload_bytes("单号,净重\n茅,100\nA,90\n".encode("gb18030"))
        self.assertEqual(resp.status_code, 303)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn("茅", day_html)

    def test_ambiguous_upload_redirects_to_choice_and_keeps_old_day(self):
        # Seed an old day first.
        self.upload_bytes(b"A,100\n", day="2026-05-01")
        resp = self.upload_bytes(self.ambiguous_bytes())
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/pending/", resp.headers["Location"])
        pending_html = self.client.get(resp.headers["Location"]).get_data(as_text=True)
        self.assertIn("首处分歧明细", pending_html)
        self.assertIn("é", pending_html)
        self.assertIn("茅", pending_html)
        # Old day still serves.
        self.assertIn(">A<", pending_html)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)
        self.assertNotIn("茅", day_html)

    def test_confirm_each_encoding_via_http(self):
        for encoding, expected in (("utf-8", "é"), ("gb18030", "茅")):
            with self.subTest(encoding=encoding):
                self.upload_bytes(b"OLDW,77\n", day="2026-05-01")
                resp = self.upload_bytes(self.ambiguous_bytes())
                location = resp.headers["Location"]
                token = location.rsplit("/", 1)[-1]
                resp = self.client.post(
                    "/confirm", data={"token": token, "encoding": encoding}
                )
                self.assertEqual(resp.status_code, 303)
                day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
                self.assertIn(f">{expected}<", day_html)
                self.assertIn(">90<", day_html)
                # The old pre-confirmation ticket is gone.
                self.assertNotIn("OLDW", day_html)

    def test_confirm_twice_second_time_requires_reupload(self):
        self.upload_bytes(b"A,100\n", day="2026-05-01")
        resp = self.upload_bytes(self.ambiguous_bytes())
        token = resp.headers["Location"].rsplit("/", 1)[-1]
        first = self.client.post(
            "/confirm", data={"token": token, "encoding": "gb18030"}
        )
        self.assertEqual(first.status_code, 303)
        second = self.client.post(
            "/confirm", data={"token": token, "encoding": "utf-8"}
        )
        self.assertEqual(second.status_code, 303)
        page = self.client.get(second.headers["Location"]).get_data(as_text=True)
        self.assertIn("重新上传", page)

    def test_expired_pending_is_gone_on_choice_page(self):
        self.upload_bytes(b"A,100\n", day="2026-05-01")
        resp = self.upload_bytes(self.ambiguous_bytes())
        token = resp.headers["Location"].rsplit("/", 1)[-1]
        # Force expiry in the DB.
        import core as core_module

        conn = core_module.connect(self.db_path)
        try:
            conn.execute("UPDATE pending_uploads SET expires_at = 0 WHERE token = ?", (token,))
        finally:
            conn.close()
        page = self.client.get(f"/pending/{token}")
        self.assertEqual(page.status_code, 303)
        follow = self.client.get(page.headers["Location"]).get_data(as_text=True)
        self.assertIn("过期", follow)
        # Old day intact.
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)

    def test_pending_json_contract(self):
        self.upload_bytes(b"A,100\n", day="2026-05-01")
        resp = self.client.post(
            "/api/upload",
            data={"date": "2026-05-01", "file": (io.BytesIO(self.ambiguous_bytes()), "f.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertTrue(body["pending"])
        self.assertTrue(body["divergence"]["utf8"]["ticket_no"] == "é")
        confirm = self.client.post(
            "/api/confirm",
            json={"token": body["token"], "encoding": "gb18030"},
        )
        self.assertEqual(confirm.status_code, 200)
        self.assertTrue(confirm.get_json()["ok"])

    def test_both_bad_upload_is_rejected_without_replacing_day(self):
        self.upload_bytes(b"A,100\n", day="2026-05-01")
        resp = self.upload_bytes(b"\xff\xff\xff\n")
        self.assertEqual(resp.status_code, 303)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class UploadValidationPageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-flask-val-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_bad_date_does_not_replace_or_leak_pool(self):
        resp = self.client.post(
            "/api/upload",
            data={
                "date": "2026-13-01",
                "file": (io.BytesIO(b"A,100\n"), "tickets.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        import core as core_module

        conn = core_module.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pending_rows").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tickets").fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_failed_validation_cleans_pool(self):
        resp = self.client.post(
            "/api/upload",
            data={
                "date": "2026-05-01",
                "file": (io.BytesIO("X,10\nX,11\n".encode()), "tickets.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        import core as core_module

        conn = core_module.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pending_rows").fetchone()[0], 0
            )
        finally:
            conn.close()
