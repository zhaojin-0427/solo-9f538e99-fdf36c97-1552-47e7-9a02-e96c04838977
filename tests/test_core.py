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


class EncodingDetectionTests(unittest.TestCase):
    """UTF-8 BOM, dual-valid files, and the reviewer-choice flow."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-enc-", suffix=".db")
        os.close(fd)
        core.init_db_path(self.db_path)
        self.conn = core.connect(self.db_path)
        self._token_seq = 0

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    class _Stream:
        def __init__(self, raw):
            self.raw = raw
            self.pos = 0

        def read(self, size=-1):
            if size is None or size < 0:
                size = len(self.raw) - self.pos
            chunk = self.raw[self.pos : self.pos + size]
            self.pos += len(chunk)
            return chunk

    def detect(self, raw, day="2026-05-01"):
        self._token_seq += 1
        token = f"tok{self._token_seq}"
        spool = core.CountingSpool(self._Stream(raw))
        try:
            result = core.detect_upload(self.conn, spool, token)
        finally:
            spool.close()
        return result

    def upload(self, raw, day="2026-05-01"):
        detection = self.detect(raw, day)
        if detection["requires_choice"]:
            return core.create_pending_upload(self.conn, day, detection, "f.csv")
        return core.replace_day_from_pool(
            self.conn, day, detection["token"], detection["chosen"]
        )

    def test_utf8_bom_is_always_utf8(self):
        raw = b"\xef\xbb\xbf" + "单号,净重\nA,100\n".encode("utf-8")
        detection = self.detect(raw)
        self.assertTrue(detection["bom"])
        self.assertFalse(detection["requires_choice"])
        self.assertEqual(detection["chosen"], "utf-8")
        self.assertEqual(len(detection["candidates"]), 1)
        summary = self.upload(raw)
        self.assertEqual(summary["new_count"], 1)
        row = self.conn.execute("SELECT ticket_no FROM tickets").fetchone()
        self.assertEqual(row["ticket_no"], "A")

    def test_bom_file_that_fails_utf8_is_rejected(self):
        # GB18030 bytes with a UTF-8 BOM: must not silently read as GB18030.
        raw = b"\xef\xbb\xbf" + "单号,净重\n茅,100\n".encode("gb18030")
        with self.assertRaises(core.ValidationError) as ctx:
            self.detect(raw)
        self.assertIn("BOM", ctx.exception.message)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM tickets").fetchone()[0], 0
        )

    def test_gb18030_only_file_imports_directly(self):
        raw = "单号,净重\n茅,100\nA,90\n".encode("gb18030")
        detection = self.detect(raw)
        self.assertFalse(detection["requires_choice"])
        self.assertEqual(detection["chosen"], "gb18030")
        summary = self.upload(raw)
        self.assertEqual(summary["new_count"], 2)
        names = {
            r["ticket_no"]
            for r in self.conn.execute("SELECT ticket_no FROM tickets")
        }
        self.assertEqual(names, {"茅", "A"})

    def test_dual_encoding_offers_first_divergence_and_waits(self):
        # GB18030 bytes for "茅,100" are valid UTF-8 as "é,100": both import,
        # but the ticket number differs. The upload must not replace the day.
        raw = "茅,100\nA,90\n".encode("gb18030")
        core.replace_day_tickets(self.conn, "2026-05-01", [("OLD", 50)])
        detection = self.detect(raw)
        self.assertTrue(detection["requires_choice"])
        self.assertEqual(detection["chosen"], None)
        div = detection["divergence"]
        self.assertEqual(div["utf8"]["ticket_no"], "é")
        self.assertEqual(div["gb18030"]["ticket_no"], "茅")
        self.assertEqual(div["row_number"], 1)

        pending = core.create_pending_upload(self.conn, "2026-05-01", detection, "f.csv")
        # Old day still serves while awaiting confirmation.
        readback = core.get_day_readback(self.conn, "2026-05-01")
        self.assertEqual([t["ticket_no"] for t in readback["tickets"]], ["OLD"])

    def test_confirm_with_each_encoding_replaces_old_day_and_holds(self):
        for chosen, expected in (("utf-8", "é"), ("gb18030", "茅")):
            with self.subTest(chosen=chosen):
                core.replace_day_tickets(self.conn, "2026-05-01", [("OLD", 50)])
                core.hold_ticket(self.conn, "2026-05-01", "OLD", "keep me")
                raw = "茅,100\nA,90\n".encode("gb18030")
                detection = self.detect(raw)
                pending = core.create_pending_upload(
                    self.conn, "2026-05-01", detection, "f.csv"
                )
                summary = core.confirm_pending_upload(
                    self.conn, pending["token"], chosen
                )
                self.assertEqual(summary["new_count"], 2)
                day = core.get_day_readback(self.conn, "2026-05-01")
                names = {t["ticket_no"] for t in day["tickets"]}
                self.assertEqual(names, {expected, "A"})
                # Hold survives; its ticket OLD is gone, so it becomes missing.
                self.assertEqual(
                    [h["ticket_no"] for h in day["missing_holds"]], ["OLD"]
                )
                # Holds on surviving tickets stay in active caliber.
                active = core.query_rank(self.conn, "2026-05-01", "A")
                self.assertFalse(active["held"])
                self.assertEqual(active["active"]["total"], 2)

    def test_confirm_expiry_keeps_old_day(self):
        raw = "茅,100\nA,90\n".encode("gb18030")
        self.detect(raw)  # prime tokens
        detection = self.detect(raw)
        pending = core.create_pending_upload(
            self.conn, "2026-05-01", detection, "f.csv", ttl_seconds=0
        )
        with self.assertRaises(core.ConfirmationExpired):
            core.confirm_pending_upload(self.conn, pending["token"], "utf-8")
        self.assertIsNone(core.get_pending_upload(self.conn, pending["token"]))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM tickets").fetchone()[0], 0
        )

    def test_day_already_replaced_requires_reupload(self):
        core.replace_day_tickets(self.conn, "2026-05-01", [("OLD", 50)])
        detection = self.detect("茅,100\nA,90\n".encode("gb18030"))
        pending = core.create_pending_upload(self.conn, "2026-05-01", detection, "f.csv")
        core.replace_day_tickets(self.conn, "2026-05-01", [("NEW", 60)])
        with self.assertRaises(core.DayAlreadyReplaced):
            core.confirm_pending_upload(self.conn, pending["token"], "gb18030")
        names = [t["ticket_no"] for t in core.get_day_readback(
            self.conn, "2026-05-01")["tickets"]]
        self.assertEqual(names, ["NEW"])

    def test_identical_dual_decoding_replaces_directly(self):
        # Pure ASCII imports under both encodings with identical details.
        raw = b"A,100\nB,90\n"
        detection = self.detect(raw)
        self.assertFalse(detection["requires_choice"])
        self.assertEqual(detection["chosen"], "utf-8")
        summary = self.upload(raw)
        self.assertEqual(summary["new_count"], 2)

    def test_neither_encoding_importable_is_rejected(self):
        raw = b"\xff\xff\xff\n"
        with self.assertRaises(core.ValidationError) as ctx:
            self.detect(raw)
        self.assertIn("拒收", ctx.exception.message)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pending_rows").fetchone()[0], 0
        )

    def test_detection_reads_at_most_twice_the_file(self):
        rows = ["单号,净重"] + [f"T{i:05d},{i % 65536}" for i in range(2000)]
        raw = "\n".join(rows).encode("utf-8")
        detection = self.detect(raw)
        self.assertLessEqual(detection["raw_bytes_read"], 2 * len(raw))
        self.assertEqual(detection["file_size"], len(raw))
        self.assertFalse(detection["requires_choice"])
        self.upload(raw)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM tickets").fetchone()[0], 2000
        )

    def test_bad_rows_do_not_leak_pool_rows(self):
        with self.assertRaises(core.ValidationError):
            self.detect("单号,净重\nX,10\nX,11\n".encode("utf-8"))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pending_rows").fetchone()[0], 0
        )


class PendingFailureTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-pend-", suffix=".db")
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

    class _Stream:
        def __init__(self, raw):
            self.raw = raw
            self.pos = 0

        def read(self, size=-1):
            if size is None or size < 0:
                size = len(self.raw) - self.pos
            chunk = self.raw[self.pos : self.pos + size]
            self.pos += len(chunk)
            return chunk

    def make_pending(self, token="tok"):
        raw = "茅,100\nA,90\n".encode("gb18030")
        spool = core.CountingSpool(self._Stream(raw))
        try:
            detection = core.detect_upload(self.conn, spool, token)
        finally:
            spool.close()
        self.assertTrue(detection["requires_choice"])
        return core.create_pending_upload(self.conn, "2026-05-01", detection, "f.csv")

    def test_hold_and_release_work_while_pending(self):
        core.replace_day_tickets(self.conn, "2026-05-01", [("OLD", 50), ("A", 90)])
        pending = self.make_pending()
        # Hold a ticket from the old day; the pending upload must not interfere.
        core.hold_ticket(self.conn, "2026-05-01", "OLD", "pre-confirm")
        self.assertTrue(
            core.get_ticket_readback(self.conn, "2026-05-01", "OLD")["held"]
        )
        core.release_ticket(self.conn, "2026-05-01", "OLD")
        self.assertFalse(
            core.get_ticket_readback(self.conn, "2026-05-01", "OLD")["held"]
        )
        # Old day still serves both calibers.
        rank = core.query_rank(self.conn, "2026-05-01", "A")
        self.assertEqual(rank["all"]["total"], 2)
        self.assertEqual(rank["active"]["total"], 2)
        # Pending is still confirmable.
        summary = core.confirm_pending_upload(self.conn, pending["token"], "gb18030")
        self.assertEqual(summary["new_count"], 2)

    def test_confirm_write_failure_keeps_old_day_holds_and_ranks(self):
        core.replace_day_tickets(self.conn, "2026-05-01", [("OLD", 50), ("B", 40)])
        core.hold_ticket(self.conn, "2026-05-01", "OLD")
        pending = self.make_pending()
        before = core.query_rank(self.conn, "2026-05-01", "B")
        core.replace_day_tickets_before = None

        original_apply = core.apply_delta

        def fail(conn, delta):
            raise RuntimeError("injected confirm failure")

        core.apply_delta = fail
        try:
            with self.assertRaises(core.ValidationError) as ctx:
                core.confirm_pending_upload(self.conn, pending["token"], "utf-8")
        finally:
            core.apply_delta = original_apply
        self.assertIn("已回滚", ctx.exception.message)

        after = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(before["all"], after["all"])
        self.assertEqual(before["active"], after["active"])
        readback = core.get_day_readback(self.conn, "2026-05-01")
        self.assertEqual(
            [(t["ticket_no"], t["net_weight"], t["held"]) for t in readback["tickets"]],
            [("B", 40, 0), ("OLD", 50, 1)],
        )
        # Pending survives a failed write and can be retried.
        self.assertIsNotNone(core.get_pending_upload(self.conn, pending["token"]))
        retry = core.confirm_pending_upload(self.conn, pending["token"], "gb18030")
        self.assertEqual(retry["new_count"], 2)

    def test_expired_pending_does_not_block_later_cleanup(self):
        pending = self.make_pending()
        with self.conn:
            self.conn.execute(
                "UPDATE pending_uploads SET expires_at = 0 WHERE token = ?",
                (pending["token"],),
            )
        with self.assertRaises(core.ConfirmationExpired):
            core.confirm_pending_upload(self.conn, pending["token"], "utf-8")
        # A fresh detection cleans up the expired pending record, and completes
        # its own direct replacement without leaving its pool behind.
        raw = b"A,100\n"
        spool = core.CountingSpool(self._Stream(raw))
        try:
            detection = core.detect_upload(self.conn, spool, "fresh")
        finally:
            spool.close()
        core.replace_day_from_pool(
            self.conn, "2026-05-01", detection["token"], detection["chosen"]
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pending_uploads").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pending_rows").fetchone()[0], 0
        )


class PoolComparisonTests(unittest.TestCase):
    """直接构造两种编码的行池，覆盖净重分歧与行数分歧。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-pool-", suffix=".db")
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

    def add_rows(self, token, encoding, rows):
        with self.conn:
            for seq, (ticket_no, weight) in enumerate(rows, start=1):
                self.conn.execute(
                    "INSERT INTO pending_rows(token, encoding, seq, ticket_no, net_weight) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (token, encoding, seq, ticket_no, weight),
                )

    def test_weight_only_divergence(self):
        self.add_rows("t", "utf-8", [("A", 100), ("B", 90)])
        self.add_rows("t", "gb18030", [("A", 100), ("B", 91)])
        div = core._compare_pools(self.conn, "t")
        self.assertEqual(div["row_number"], 2)
        self.assertEqual(div["utf8"]["ticket_no"], "B")
        self.assertEqual(div["utf8"]["net_weight"], 90)
        self.assertEqual(div["gb18030"]["net_weight"], 91)

    def test_trailing_row_divergence(self):
        self.add_rows("t", "utf-8", [("A", 100)])
        self.add_rows("t", "gb18030", [("A", 100), ("B", 90)])
        div = core._compare_pools(self.conn, "t")
        self.assertEqual(div["row_number"], 2)
        self.assertTrue(div["utf8"]["missing"])
        self.assertEqual(div["gb18030"]["ticket_no"], "B")

    def test_identical_pools_have_no_divergence(self):
        self.add_rows("t", "utf-8", [("A", 100), ("B", 90)])
        self.add_rows("t", "gb18030", [("A", 100), ("B", 90)])
        self.assertIsNone(core._compare_pools(self.conn, "t"))
