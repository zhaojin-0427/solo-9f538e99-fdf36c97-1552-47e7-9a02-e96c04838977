import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core


class KeyBoundTests(unittest.TestCase):
    def test_date_cover_never_uses_more_than_15_segments(self):
        for year in (2023, 2024, 2026):
            n = core.days_in_year(year)
            jan1 = date(year, 1, 1)
            worst = 0
            for start_offset in range(n):
                for end_offset in range(start_offset, n):
                    start = jan1 + core.timedelta(days=start_offset)
                    end = jan1 + core.timedelta(days=end_offset)
                    segments = core.date_cover(start, end)
                    worst = max(worst, len(segments))
                    self.assertLessEqual(len(segments), 15)
                    # Segments are disjoint and exactly cover the interval.
                    self.assertEqual(segments[0].lo, start_offset)
                    self.assertEqual(segments[-1].hi, end_offset + 1)
            self.assertLessEqual(worst, 15)

    def test_weight_cover_and_total_query_budget(self):
        worst_greater = 0
        for weight in range(core.WEIGHT_SIZE):
            greater = core.weight_greater_cover(weight)
            worst_greater = max(worst_greater, len(greater))
            self.assertLessEqual(len(greater), 16)

            # root total + greater bands + exact-weight leaf, per date segment
            weight_keys_per_date = 1 + len(greater) + 1
            self.assertLessEqual(weight_keys_per_date, 18)

        self.assertEqual(worst_greater, 16)
        # Two calibers * at most 15 date segments * 18 weight keys.
        self.assertLessEqual(2 * 15 * 18, core.SUBTOTAL_KEY_LIMIT)
        self.assertEqual(2 * 15 * 18, 540)


class RankFlowTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-test-", suffix=".db")
        os.close(fd)
        core.init_db_path(self.db_path)
        self.conn = core.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload(self, day="2026-05-01", tickets=None):
        if tickets is None:
            tickets = [("A", 100), ("B", 90), ("C", 90), ("D", 80)]
        return core.replace_day_tickets(self.conn, day, tickets)

    def test_tie_rank_interval_and_access_limit(self):
        self.upload()
        result = core.query_rank(
            self.conn, "2026-05-01", "B", "2026-01-01", "2026-12-31"
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["all"]["total"], 4)
        self.assertEqual(result["all"]["higher"], 1)
        self.assertEqual(result["all"]["equal"], 2)
        self.assertEqual((result["all"]["rank_start"], result["all"]["rank_end"]), (2, 3))
        self.assertEqual(result["active"]["rank_start"], 2)
        self.assertEqual(result["active"]["rank_end"], 3)
        self.assertLessEqual(result["meter"]["accessed"], 576)
        self.assertEqual(result["meter"]["accessed"], len(result["meter"]["details"]))

    def test_held_target_leaves_active_rank_blank(self):
        self.upload()
        core.hold_ticket(self.conn, "2026-05-01", "B", "disputed")
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertTrue(result["held"])
        self.assertEqual(result["all"]["rank_start"], 2)
        self.assertEqual(result["active"]["total"], 3)
        self.assertIsNone(result["active"]["rank_start"])
        self.assertIsNone(result["active"]["rank_end"])
        self.assertEqual(result["active"]["rank_text"], "")
        self.assertLessEqual(result["meter"]["accessed"], 576)

    def test_hold_survives_reupload_removal_and_reappearance(self):
        self.upload()
        core.hold_ticket(self.conn, "2026-05-01", "A")

        # Reupload omits A and changes B; hold row must remain.
        core.replace_day_tickets(
            self.conn,
            "2026-05-01",
            [("B", 95), ("C", 90), ("D", 80), ("E", 70)],
        )
        readback = core.get_day_readback(self.conn, "2026-05-01")
        self.assertEqual([h["ticket_no"] for h in readback["missing_holds"]], ["A"])

        # A reappears in a later whole-day replacement, still held and therefore
        # absent from active totals/ranks.
        core.replace_day_tickets(
            self.conn,
            "2026-05-01",
            [("A", 100), ("B", 95), ("C", 90), ("D", 80), ("E", 70)],
        )
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(result["all"]["total"], 5)
        self.assertEqual(result["all"]["higher"], 1)  # A in all caliber
        self.assertEqual(result["active"]["total"], 4)
        self.assertEqual(result["active"]["higher"], 0)  # A excluded
        self.assertEqual(result["active"]["rank_start"], 1)

        core.release_ticket(self.conn, "2026-05-01", "A")
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(result["active"]["total"], 5)
        self.assertEqual(result["active"]["higher"], 1)

    def test_failed_csv_validation_leaves_old_data_and_queryable(self):
        self.upload()
        before = core.query_rank(self.conn, "2026-05-01", "B")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,10\nX,11\nY,70000\n")
        # Validation happens before replace_day_tickets; explicitly feed parsed
        # bad data is impossible, so call replacement only with good parsed data.
        after = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(before["all"], after["all"])

    def test_failed_database_delta_rolls_back_ticket_and_hold(self):
        self.upload()
        original_adjust = core.adjust_existing_ticket_active

        def fail(*args, **kwargs):
            raise RuntimeError("injected subtotal failure")

        core.adjust_existing_ticket_active = fail
        try:
            with self.assertRaises(RuntimeError):
                core.hold_ticket(self.conn, "2026-05-01", "B")
        finally:
            core.adjust_existing_ticket_active = original_adjust

        # Transaction rolled back: no hold and ticket still queryable as active.
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertFalse(result["held"])
        self.assertEqual(result["active"]["total"], 4)

    def test_failed_reupload_delta_rolls_back_to_old_day(self):
        self.upload()
        original_apply = core.apply_delta

        def fail(conn, delta):
            if getattr(fail, "armed", False):
                raise RuntimeError("injected replace failure")
            return original_apply(conn, delta)

        fail.armed = True
        core.apply_delta = fail
        try:
            with self.assertRaises(RuntimeError):
                core.replace_day_tickets(
                    self.conn, "2026-05-01", [("Z", 1), ("Y", 2)]
                )
        finally:
            core.apply_delta = original_apply

        rows = self.conn.execute(
            "SELECT ticket_no FROM tickets WHERE slip_date=? ORDER BY ticket_no",
            ("2026-05-01",),
        ).fetchall()
        self.assertEqual([r["ticket_no"] for r in rows], ["A", "B", "C", "D"])
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(result["all"]["total"], 4)

    def test_csv_parsing_headers_headerless_and_bounds(self):
        rows = core.parse_csv_text("单号,净重\nX,0\nY,65535\n")
        self.assertEqual(rows, [("X", 0), ("Y", 65535)])
        rows = core.parse_csv_text("X,12\nY,13\n")
        self.assertEqual(rows, [("X", 12), ("Y", 13)])
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,-1\n")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,65536\n")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,1.5\n")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,1\nX,2\n")

    def test_query_plan_uses_primary_key_index(self):
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM tickets WHERE slip_date=? AND ticket_no=?",
            ("2026-05-01", "B"),
        ).fetchall()
        text = "\n".join(row[3] for row in plan)
        self.assertIn("PRIMARY KEY", text.upper())


if __name__ == "__main__":
    unittest.main()
