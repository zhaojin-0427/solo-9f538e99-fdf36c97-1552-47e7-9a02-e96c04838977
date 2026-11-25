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
