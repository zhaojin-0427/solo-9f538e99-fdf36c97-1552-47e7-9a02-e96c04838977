"""Core domain, SQLite persistence, subtotal keys, and rank queries.

This module is framework-neutral so the important guarantees can be tested
without Flask.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Iterator, Optional


WEIGHT_MIN = 0
WEIGHT_MAX = 65535
WEIGHT_SIZE = 65536  # 2 ** 16
SUBTOTAL_KEY_LIMIT = 576

SCOPE_ALL = "all"
SCOPE_ACTIVE = "active"


class ValidationError(ValueError):
    """Invalid user input. Messages are safe to show on the review page."""

    def __init__(self, message: str, errors: Optional[Iterable[str]] = None):
        super().__init__(message)
        self.message = message
        self.errors = list(errors or [])


# ---------------------------------------------------------------------------
# Date and weight canonical segments
# ---------------------------------------------------------------------------


def days_in_year(year: int) -> int:
    return 366 if date(year, 12, 31).timetuple().tm_yday == 366 else 365


def parse_iso_date(value: str, field_name: str = "日期") -> date:
    if value is None:
        raise ValidationError(f"{field_name}不能为空")
    text = str(value).strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValidationError(f"{field_name}必须是 YYYY-MM-DD 格式")
    try:
        year = int(text[0:4])
        month = int(text[5:7])
        day = int(text[8:10])
        return date(year, month, day)
    except ValueError as exc:
        raise ValidationError(f"{field_name}不是有效日期：{text}") from exc


@dataclass(frozen=True)
class DateSeg:
    year: int
    lo: int  # zero-based day-of-year offset, inclusive
    hi: int  # zero-based offset, exclusive

    @property
    def start(self) -> date:
        return date(self.year, 1, 1) + timedelta(days=self.lo)

    @property
    def end_inclusive(self) -> date:
        return date(self.year, 1, 1) + timedelta(days=self.hi - 1)

    @property
    def key(self) -> str:
        return f"date:{self.start.isoformat()}..{self.end_inclusive.isoformat()}"

    @property
    def label(self) -> str:
        if self.lo + 1 == self.hi:
            return self.start.isoformat()
        return f"{self.start.isoformat()} ~ {self.end_inclusive.isoformat()}"


def _date_seg(year: int, lo: int, hi: int) -> DateSeg:
    return DateSeg(year=year, lo=lo, hi=hi)


def date_ancestors(day: date) -> list[DateSeg]:
    """Balanced-tree segments from the full year down to one day."""
    year = day.year
    n = days_in_year(year)
    idx = (day - date(year, 1, 1)).days
    lo, hi = 0, n
    result: list[DateSeg] = []
    while True:
        result.append(_date_seg(year, lo, hi))
        if hi - lo == 1:
            return result
        mid = (lo + hi) // 2
        if idx < mid:
            hi = mid
        else:
            lo = mid


def date_cover(start: date, end: date) -> list[DateSeg]:
    """Canonical balanced-tree segments covering [start, end] inclusively."""
    if start.year != end.year:
        raise ValidationError("查询起止日必须在同一年")
    if start > end:
        raise ValidationError("起始日不能晚于结束日")

    year = start.year
    n = days_in_year(year)
    lo = (start - date(year, 1, 1)).days
    hi = (end - date(year, 1, 1)).days + 1
    if not (0 <= lo < hi <= n):
        raise ValidationError("日期超出该年范围")

    found: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = [(0, n)]
    while stack:
        a, b = stack.pop()
        if hi <= a or b <= lo:
            continue
        if lo <= a and b <= hi:
            found.append((a, b))
            continue
        mid = (a + b) // 2
        stack.append((a, mid))
        stack.append((mid, b))

    found.sort()
    return [_date_seg(year, a, b) for a, b in found]


@dataclass(frozen=True)
class WeightSeg:
    lo: int  # inclusive
    hi: int  # exclusive

    @property
    def key(self) -> str:
        return f"weight:{self.lo:05d}..{self.hi - 1:05d}"

    @property
    def label(self) -> str:
        if self.hi - self.lo == 1:
            return f"{self.lo} kg"
        return f"{self.lo}–{self.hi - 1} kg"


def weight_ancestors(weight: int) -> list[WeightSeg]:
    """Dyadic segments from the exact weight leaf up to the 0..65535 root."""
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValidationError("净重必须在 0～65535 之间")

    lo = weight
    size = 1
    result = [WeightSeg(lo, lo + size)]
    while size < WEIGHT_SIZE:
        size *= 2
        lo = (lo // size) * size
        result.append(WeightSeg(lo, lo + size))
    return result


def weight_cover(lo: int, hi: int) -> list[WeightSeg]:
    """Minimal aligned dyadic cover of the half-open range [lo, hi)."""
    if lo >= hi:
        return []
    if not (0 <= lo < hi <= WEIGHT_SIZE):
        raise ValidationError("重量区间超出 0～65535")

    result: list[WeightSeg] = []
    pos = lo
    while pos < hi:
        size = 1
        while size * 2 <= hi - pos and pos % (size * 2) == 0:
            size *= 2
        result.append(WeightSeg(pos, pos + size))
        pos += size
    return result


def weight_greater_cover(weight: int) -> list[WeightSeg]:
    return weight_cover(weight + 1, WEIGHT_SIZE)


# ---------------------------------------------------------------------------
# SQLite schema and transaction helpers
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS tickets (
    slip_date   TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    net_weight  INTEGER NOT NULL CHECK (net_weight BETWEEN 0 AND 65535),
    PRIMARY KEY (slip_date, ticket_no)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS holds (
    slip_date   TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slip_date, ticket_no)
) WITHOUT ROWID;

-- One row = one date segment × one weight band × one caliber.
CREATE TABLE IF NOT EXISTS subtotals (
    date_key     TEXT NOT NULL,
    weight_key   TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('all', 'active')),
    ticket_count INTEGER NOT NULL CHECK (ticket_count >= 0),
    PRIMARY KEY (date_key, weight_key, scope)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_holds_date ON holds(slip_date);

-- Monotonic per-day report revision. Incremented once per committed
-- whole-day replacement; a pending encoding confirmation is bound to the
-- revision it was prepared from.
CREATE TABLE IF NOT EXISTS day_state (
    slip_date   TEXT NOT NULL PRIMARY KEY,
    revision    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
) WITHOUT ROWID;

-- A file that is valid under both UTF-8 and GB18030 with different details.
-- Nothing here touches tickets/subtotals until the reviewer confirms, so the
-- old daily report keeps serving while the choice is pending.
CREATE TABLE IF NOT EXISTS pending_uploads (
    token         TEXT NOT NULL PRIMARY KEY,
    slip_date     TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    filename      TEXT,
    file_size     INTEGER NOT NULL,
    raw_bytes_read INTEGER NOT NULL,
    bom           INTEGER NOT NULL DEFAULT 0,
    candidates_json TEXT NOT NULL,
    divergence_json TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_pending_expiry ON pending_uploads(expires_at);

-- Parsed rows for each encoding candidate, kept on disk. A unique key on the
-- ticket number enforces per-file uniqueness and duplicate detection without
-- holding every ticket number in Python memory.
CREATE TABLE IF NOT EXISTS pending_rows (
    token      TEXT NOT NULL,
    encoding   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ticket_no  TEXT NOT NULL,
    net_weight INTEGER NOT NULL CHECK (net_weight BETWEEN 0 AND 65535),
    PRIMARY KEY (token, encoding, ticket_no)
) WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_rows_seq
    ON pending_rows(token, encoding, seq);

-- Every row pool, including in-flight detections that have no pending_uploads
-- row yet. Used only to sweep pools abandoned by a crashed request; entries
-- younger than the pending TTL are never touched, so two concurrent uploads
-- cannot delete each other's work.
CREATE TABLE IF NOT EXISTS pending_tokens (
    token      TEXT NOT NULL PRIMARY KEY,
    created_at REAL NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_pending_tokens_created ON pending_tokens(created_at);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def init_db_path(db_path: str) -> None:
    if db_path != ":memory:":
        import os

        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

TICKET_HEADERS = {
    "单号",
    "秤单号",
    "提单号",
    "票号",
    "车单号",
    "ticket",
    "ticketno",
    "ticket_no",
    "id",
    "no",
}
WEIGHT_HEADERS = {
    "净重",
    "净重kg",
    "净重(kg)",
    "净重（kg）",
    "重量",
    "weight",
    "netweight",
    "net_weight",
    "kg",
}


def _normalise_header(value: object) -> str:
    return "".join(str(value or "").strip().lower().replace(" ", "").split())


def parse_weight(value: object, row_number: int) -> int:
    text = str(value if value is not None else "").strip()
    compact = text.replace(",", "").replace("_", "")
    if not compact or not (compact[0] in "+-" and compact[1:].isdigit() or compact.isdigit()):
        raise ValidationError(f"第 {row_number} 行净重不是整数：{text!r}")
    weight = int(compact)
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValidationError(
            f"第 {row_number} 行净重 {weight} 超出范围（0～65535 千克）"
        )
    return weight


def parse_csv_text(text: str) -> list[tuple[str, int]]:
    if text is None or not text.strip():
        raise ValidationError("CSV 为空，请选择包含“单号,净重”数据的文件")
    return parse_csv_stream(io.StringIO(text))


def parse_csv_stream(stream) -> list[tuple[str, int]]:
    """Parse a text stream of CSV rows. Only one row is held at a time."""
    tickets: list[tuple[str, int]] = []
    seen: set[str] = set()

    def accept(ticket_no: str, weight: int, line_no: int) -> Optional[str]:
        if ticket_no in seen:
            return f"第 {line_no} 行单号在同一 CSV 内重复：{ticket_no}"
        seen.add(ticket_no)
        tickets.append((ticket_no, weight))
        return None

    _stream_tickets(stream, accept)
    return tickets


def _stream_tickets(stream, accept, max_errors: int = 50) -> int:
    """Stream CSV rows through ``accept(ticket_no, weight, line_no)``.

    ``accept`` returns an error message to reject a row, or ``None`` to keep
    it. Only the first ``max_errors`` messages are retained, so memory does
    not grow with bad-row count. Returns the number of accepted rows.
    """
    reader = _make_reader(stream)
    errors: list[str] = []
    error_count = 0
    imported = 0

    def add_error(message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < max_errors:
            errors.append(message)

    first_row = _next_data_row(reader)
    if first_row is None:
        raise ValidationError("CSV 没有有效数据行")

    first = [_normalise_header(cell) for cell in first_row]
    ticket_idx = next((i for i, h in enumerate(first) if h in TICKET_HEADERS), None)
    weight_idx = next((i for i, h in enumerate(first) if h in WEIGHT_HEADERS), None)

    pending: list[list[str]]
    if ticket_idx is not None and weight_idx is not None:
        pending = []
        start_line = 2
    else:
        # Headerless format is explicitly supported: first two columns are
        # ticket number then net weight.
        ticket_idx, weight_idx = 0, 1
        pending = [first_row]
        start_line = 1

    line_no = start_line
    for row in _iter_parsed_rows(reader, pending):
        if len(row) <= max(ticket_idx, weight_idx):
            add_error(f"第 {line_no} 行缺少单号或净重：{row!r}")
            line_no += 1
            continue

        ticket_no = str(row[ticket_idx]).strip()
        weight_text = row[weight_idx]
        if not ticket_no:
            add_error(f"第 {line_no} 行单号为空")
            line_no += 1
            continue
        try:
            weight = parse_weight(weight_text, line_no)
        except ValidationError as exc:
            add_error(exc.message)
            line_no += 1
            continue

        message = accept(ticket_no, weight, line_no)
        if message:
            add_error(message)
        else:
            imported += 1
        line_no += 1

    if error_count:
        raise ValidationError(
            f"CSV 校验未通过（{error_count} 处问题），未写入任何数据",
            errors=errors,
        )
    if not imported:
        raise ValidationError("CSV 没有可导入的数据行")
    return imported


def _make_reader(stream):
    """Build a CSV reader, sniffing the delimiter from a bounded prefix."""
    prefix = stream.read(4096)
    try:
        dialect = csv.Sniffer().sniff(prefix, delimiters=",;\t")
        return csv.reader(_prefixed_lines(prefix, stream), dialect)
    except csv.Error:
        return csv.reader(_prefixed_lines(prefix, stream))


def _prefixed_lines(prefix: str, rest) -> Iterator[str]:
    """Yield physical lines from a bounded prefix followed by the rest.

    Only CR/LF count as line breaks so quoted multi-line fields keep working.
    """
    carry = ""
    if prefix:
        lines, carry = _split_physical_lines(prefix)
        yield from lines
    for line in rest:
        if carry:
            line = carry + line
            carry = ""
        yield line
    if carry:
        yield carry


def _split_physical_lines(text: str) -> tuple[list[str], str]:
    lines: list[str] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\n":
            lines.append(text[start : index + 1])
            start = index + 1
        elif char == "\r":
            if index + 1 < length and text[index + 1] == "\n":
                index += 1
            lines.append(text[start : index + 1])
            start = index + 1
        index += 1
    return lines, text[start:]


def _next_data_row(reader) -> Optional[list[str]]:
    for row in reader:
        if any(str(cell).strip() for cell in row):
            return row
    return None


def _iter_parsed_rows(reader, pending):
    yield from pending
    for row in reader:
        if any(str(cell).strip() for cell in row):
            yield row


# ---------------------------------------------------------------------------
# Streaming dual-encoding detection
# ---------------------------------------------------------------------------

ENCODING_UTF8 = "utf-8"
ENCODING_GB18030 = "gb18030"
CANDIDATE_ENCODINGS = (ENCODING_UTF8, ENCODING_GB18030)
BOM_UTF8 = codecs.BOM_UTF8
_UPLOAD_CHUNK = 64 * 1024
MAX_CANDIDATE_ERRORS = 20
PENDING_TTL_SECONDS = 1800


@dataclass
class Candidate:
    """Result of validating one encoding for one upload."""

    encoding: str
    ok: bool
    ticket_count: int = 0
    error: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "encoding": self.encoding,
            "ok": self.ok,
            "ticket_count": self.ticket_count,
            "error": self.error,
            "errors": self.errors[:MAX_CANDIDATE_ERRORS],
        }


def _decode_error_text(exc: UnicodeDecodeError) -> str:
    """Short, UI-safe description of a decode failure."""
    return f"文件第 {exc.start + 1} 字节起无法按该编码读取"


class CountingSpool:
    """Spool uploaded bytes to disk once, then decode from that spool.

    "Uploaded original bytes" are accounted in two segments: the client body
    is read once into the spool (``size``), and the spool is then read once to
    feed the candidate decoders (``bytes_delivered``). ``total_bytes_read`` is
    therefore exactly 2x the file size in the worst case, never more. The
    final replacement consumes the parsed on-disk row pool instead of the raw
    bytes. The spool spills to disk above 1 MiB, so working memory does not
    grow with the ticket count.
    """

    def __init__(self, data_stream):
        self._stream = data_stream
        self._file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        self.size = 0
        self.bytes_delivered = 0
        self.has_bom = False
        self._spooled = False

    @property
    def total_bytes_read(self) -> int:
        """Client bytes plus spool bytes handed to decoding (≤ 2x file size)."""
        return self.size + self.bytes_delivered

    def spool(self) -> None:
        if self._spooled:
            return
        head = b""
        while True:
            chunk = self._stream.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            if len(head) < len(BOM_UTF8):
                # Guard against a stream returning the BOM one byte at a time.
                head += chunk[: len(BOM_UTF8) - len(head)]
            self.size += len(chunk)
            self._file.write(chunk)
        self.has_bom = head.startswith(BOM_UTF8)
        self._file.seek(0)
        self._spooled = True

    def read_chunks(self):
        self.spool()
        self._file.seek(0)
        while True:
            chunk = self._file.read(_UPLOAD_CHUNK)
            if not chunk:
                return
            self.bytes_delivered += len(chunk)
            yield chunk

    def close(self) -> None:
        self._file.close()


class _DecodedText:
    """Decoded text spilled to disk, bounded in memory by a 1 MiB buffer."""

    def __init__(self):
        self._file = tempfile.SpooledTemporaryFile(
            max_size=1024 * 1024, mode="w+", encoding="utf-8", newline=""
        )

    def write(self, text: str) -> None:
        self._file.write(text)

    def open(self):
        self._file.seek(0)
        return self._file

    def close(self) -> None:
        self._file.close()


def _decode_candidates(
    spool: CountingSpool, encodings: list[str], has_bom: bool
) -> tuple[dict[str, _DecodedText], dict[str, Optional[str]]]:
    """Decode every candidate encoding from one pass over the uploaded bytes.

    Returns the decoded text spools and any per-encoding decode error. A
    decoder that fails early simply stops accumulating text; the raw stream is
    still read once for the remaining candidates.
    """
    codecs_for = {
        enc: ("utf-8-sig" if has_bom and enc == ENCODING_UTF8 else enc)
        for enc in encodings
    }
    decoders = {
        enc: codecs.getincrementaldecoder(codecs_for[enc])("strict") for enc in encodings
    }
    texts = {enc: _DecodedText() for enc in encodings}
    errors: dict[str, Optional[str]] = {enc: None for enc in encodings}

    for raw_chunk in spool.read_chunks():
        for enc in encodings:
            if errors[enc] is not None:
                continue
            try:
                text = decoders[enc].decode(raw_chunk)
            except UnicodeDecodeError as exc:
                errors[enc] = _decode_error_text(exc)
                continue
            if text:
                texts[enc].write(text)

    for enc in encodings:
        if errors[enc] is not None:
            continue
        try:
            tail = decoders[enc].decode(b"", final=True)
        except UnicodeDecodeError as exc:
            errors[enc] = _decode_error_text(exc)
            continue
        if tail:
            texts[enc].write(tail)

    return texts, errors


def _insert_row(conn, token: str, encoding: str, seq: int, ticket_no: str, weight: int) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO pending_rows(token, encoding, seq, ticket_no, net_weight)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token, encoding, seq, ticket_no, weight),
    )
    return cursor.rowcount == 1


def _build_candidate(
    conn: sqlite3.Connection,
    text_stream,
    token: str,
    encoding: str,
) -> Candidate:
    """Parse one decoded text stream, spooling accepted rows into pending_rows.

    Duplicate ticket numbers are rejected by the pool's primary key, so no
    Python set of ticket numbers is kept and memory stays flat as the file
    grows.
    """
    with write_transaction(conn):
        _delete_pool(conn, token, encoding)
    state = {"seq": 0}

    def accept(ticket_no: str, weight: int, line_no: int) -> Optional[str]:
        state["seq"] += 1
        if not _insert_row(conn, token, encoding, state["seq"], ticket_no, weight):
            return f"第 {line_no} 行单号在同一 CSV 内重复：{ticket_no}"
        return None

    try:
        with write_transaction(conn):
            count = _stream_tickets(text_stream, accept)
    except ValidationError as exc:
        with write_transaction(conn):
            _delete_pool(conn, token, encoding)
        return Candidate(
            encoding=encoding, ok=False, error=exc.message, errors=list(exc.errors)
        )
    return Candidate(encoding=encoding, ok=True, ticket_count=count)


def _pool_rows(conn: sqlite3.Connection, token: str, encoding: str):
    return conn.execute(
        """
        SELECT seq, ticket_no, net_weight
        FROM pending_rows
        WHERE token = ? AND encoding = ?
        ORDER BY seq
        """,
        (token, encoding),
    )


def _compare_pools(conn: sqlite3.Connection, token: str) -> Optional[dict[str, object]]:
    """First diverging detail between the two pools, streamed row by row."""
    utf8_rows = _pool_rows(conn, token, ENCODING_UTF8)
    gb_rows = _pool_rows(conn, token, ENCODING_GB18030)
    index = 0
    while True:
        a = utf8_rows.fetchone()
        b = gb_rows.fetchone()
        if a is None and b is None:
            return None
        index += 1
        a_detail = (
            {"ticket_no": a["ticket_no"], "net_weight": int(a["net_weight"])}
            if a is not None
            else None
        )
        b_detail = (
            {"ticket_no": b["ticket_no"], "net_weight": int(b["net_weight"])}
            if b is not None
            else None
        )
        if a_detail == b_detail:
            continue
        return {
            "row_number": index,
            "utf8": a_detail or {"ticket_no": None, "net_weight": None, "missing": True},
            "gb18030": b_detail or {"ticket_no": None, "net_weight": None, "missing": True},
        }


def _cleanup_expired_pending(conn: sqlite3.Connection, keep_token: str) -> None:
    """Sweep expired pending records and abandoned row pools.

    Only pools whose token is older than the pending TTL and not bound to a
    live pending record are removed, so a concurrent upload that is still
    being detected is never touched.
    """
    now = time.time()
    expired = conn.execute(
        "SELECT token FROM pending_uploads WHERE expires_at <= ?", (now,)
    ).fetchall()
    for row in expired:
        conn.execute("DELETE FROM pending_rows WHERE token = ?", (row["token"],))
        conn.execute("DELETE FROM pending_tokens WHERE token = ?", (row["token"],))
    conn.execute("DELETE FROM pending_uploads WHERE expires_at <= ?", (now,))

    cutoff = now - PENDING_TTL_SECONDS
    stale = conn.execute(
        """
        SELECT t.token
        FROM pending_tokens AS t
        WHERE t.created_at <= ?
          AND t.token != ?
          AND NOT EXISTS (
              SELECT 1 FROM pending_uploads AS p WHERE p.token = t.token
          )
        """,
        (cutoff, keep_token),
    ).fetchall()
    for row in stale:
        conn.execute("DELETE FROM pending_rows WHERE token = ?", (row["token"],))
        conn.execute("DELETE FROM pending_tokens WHERE token = ?", (row["token"],))
    conn.execute(
        "DELETE FROM pending_tokens WHERE created_at <= ? AND token != ?",
        (cutoff, keep_token),
    )


def _register_pool_token(conn: sqlite3.Connection, token: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pending_tokens(token, created_at) VALUES (?, ?)",
        (token, time.time()),
    )


def detect_upload(
    conn: sqlite3.Connection,
    spool: CountingSpool,
    token: str,
) -> dict[str, object]:
    """Decide whether one upload needs an encoding choice.

    A UTF-8 BOM means UTF-8 only. Otherwise UTF-8 and GB18030 are decoded from
    the same single pass over the uploaded bytes, then parsed independently.
    If both import and the first differing ticket detail exists, the reviewer
    must choose. If only one imports, or both yield identical details, it can
    be replaced directly.
    """
    spool.spool()
    has_bom = spool.has_bom
    encodings = [ENCODING_UTF8] if has_bom else list(CANDIDATE_ENCODINGS)

    with write_transaction(conn):
        _cleanup_expired_pending(conn, keep_token=token)
        _register_pool_token(conn, token)

    decoded, decode_errors = _decode_candidates(spool, encodings, has_bom)
    candidates: list[Candidate] = []
    try:
        for enc in encodings:
            if decode_errors[enc] is not None:
                candidates.append(
                    Candidate(encoding=enc, ok=False, error=decode_errors[enc])
                )
                continue
            candidates.append(
                _build_candidate(conn, decoded[enc].open(), token, enc)
            )
    finally:
        for text in decoded.values():
            text.close()

    importable = [c for c in candidates if c.ok]
    result: dict[str, object] = {
        "token": token,
        "bom": has_bom,
        "requires_choice": False,
        "chosen": None,
        "candidates": candidates,
        "divergence": None,
        "raw_bytes_read": spool.total_bytes_read,
        "file_size": spool.size,
    }

    if not importable:
        with write_transaction(conn):
            _delete_pool(conn, token)
        errors: list[str] = []
        for candidate in candidates:
            label = "UTF-8" if candidate.encoding == ENCODING_UTF8 else "GB18030"
            errors.append(f"{label}：{candidate.error or '无法入库'}")
            errors.extend(candidate.errors[:MAX_CANDIDATE_ERRORS])
        if has_bom:
            raise ValidationError(
                "文件带 UTF-8 BOM，只能按 UTF-8 读取，但该文件无法入库，已拒收",
                errors=errors,
            )
        raise ValidationError(
            "文件按 UTF-8 和 GB18030 都无法入库，已拒收", errors=errors
        )

    if len(importable) == 2:
        divergence = _compare_pools(conn, token)
        if divergence is not None:
            result["requires_choice"] = True
            result["divergence"] = divergence
            result["raw_bytes_read"] = spool.total_bytes_read
            return result

    chosen = next(
        (c.encoding for c in candidates if c.encoding == ENCODING_UTF8 and c.ok),
        importable[0].encoding,
    )
    with write_transaction(conn):
        for encoding in CANDIDATE_ENCODINGS:
            if encoding != chosen:
                _delete_pool(conn, token, encoding)
    result["chosen"] = chosen
    result["raw_bytes_read"] = spool.total_bytes_read
    return result


def _delete_pool(conn: sqlite3.Connection, token: str, encoding: Optional[str] = None) -> None:
    if encoding is None:
        conn.execute("DELETE FROM pending_rows WHERE token = ?", (token,))
        conn.execute("DELETE FROM pending_tokens WHERE token = ?", (token,))
    else:
        conn.execute(
            "DELETE FROM pending_rows WHERE token = ? AND encoding = ?",
            (token, encoding),
        )


# ---------------------------------------------------------------------------
# Aggregate updates
# ---------------------------------------------------------------------------


def _add_delta(delta: dict[tuple[str, str, str], int], key: tuple[str, str, str], value: int) -> None:
    delta[key] = delta.get(key, 0) + value


def add_ticket_to_delta(
    delta: dict[tuple[str, str, str], int],
    slip_date: str | date,
    weight: int,
    held: bool,
    sign: int,
    include_all: bool = True,
    include_active: bool | None = None,
) -> None:
    day = slip_date if isinstance(slip_date, date) else parse_iso_date(slip_date, "单据日期")
    date_keys = [seg.key for seg in date_ancestors(day)]
    weight_keys = [seg.key for seg in weight_ancestors(weight)]
    if include_active is None:
        include_active = not held

    for date_key in date_keys:
        for weight_key in weight_keys:
            if include_all:
                _add_delta(delta, (date_key, weight_key, SCOPE_ALL), sign)
            if include_active:
                _add_delta(delta, (date_key, weight_key, SCOPE_ACTIVE), sign)


def apply_delta(conn: sqlite3.Connection, delta: dict[tuple[str, str, str], int]) -> int:
    changes = 0
    for (date_key, weight_key, scope), amount in delta.items():
        if amount == 0:
            continue
        if amount > 0:
            conn.execute(
                """
                INSERT INTO subtotals(date_key, weight_key, scope, ticket_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date_key, weight_key, scope) DO UPDATE SET
                    ticket_count = subtotals.ticket_count + excluded.ticket_count
                """,
                (date_key, weight_key, scope, amount),
            )
        else:
            # Do not INSERT a negative proposed count: SQLite can evaluate the
            # CHECK before UPSERT conflict handling. A decremented key must
            # already exist because the corresponding original ticket exists.
            cursor = conn.execute(
                """
                UPDATE subtotals
                SET ticket_count = ticket_count + ?
                WHERE date_key = ? AND weight_key = ? AND scope = ?
                  AND ticket_count + ? >= 0
                """,
                (amount, date_key, weight_key, scope, amount),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    f"缺少或无法扣减小计键：{date_key} / {weight_key} / {scope} / {amount}"
                )
        changes += 1
    conn.execute("DELETE FROM subtotals WHERE ticket_count = 0")
    return changes


def adjust_existing_ticket_active(
    conn: sqlite3.Connection, slip_date: str, weight: int, sign: int
) -> None:
    delta: dict[tuple[str, str, str], int] = {}
    add_ticket_to_delta(
        delta,
        slip_date,
        weight,
        held=False,
        sign=sign,
        include_all=False,
        include_active=True,
    )
    apply_delta(conn, delta)


# ---------------------------------------------------------------------------
# Upload / replacement and holds
# ---------------------------------------------------------------------------


def _day_revision(conn: sqlite3.Connection, date_text: str) -> int:
    row = conn.execute(
        "SELECT revision FROM day_state WHERE slip_date = ?", (date_text,)
    ).fetchone()
    return int(row["revision"]) if row is not None else 0


def _bump_revision(conn: sqlite3.Connection, date_text: str) -> int:
    conn.execute(
        """
        INSERT INTO day_state(slip_date, revision, updated_at)
        VALUES (?, 1, datetime('now'))
        ON CONFLICT(slip_date) DO UPDATE SET
            revision = day_state.revision + 1,
            updated_at = datetime('now')
        """,
        (date_text,),
    )
    return _day_revision(conn, date_text)


def get_day_revision(conn: sqlite3.Connection, slip_date: str) -> int:
    day = parse_iso_date(slip_date, "日期").isoformat()
    return _day_revision(conn, day)


def format_local_time(timestamp: float) -> str:
    """Render a Unix timestamp in the server's local time for display."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _replace_day_rows(
    conn: sqlite3.Connection,
    date_text: str,
    new_rows: Iterable[tuple[str, int]],
) -> dict[str, object]:
    """Whole-day replacement from an iterable of (ticket_no, net_weight).

    Must run inside a write transaction. Reads old tickets and holds, applies
    the delta, rewrites tickets, and bumps the day revision. Any exception
    leaves the previous committed day untouched.
    """
    old_rows = conn.execute(
        "SELECT ticket_no, net_weight FROM tickets WHERE slip_date = ?",
        (date_text,),
    ).fetchall()
    held_rows = conn.execute(
        "SELECT ticket_no FROM holds WHERE slip_date = ?",
        (date_text,),
    ).fetchall()
    held_numbers = {row["ticket_no"] for row in held_rows}

    delta: dict[tuple[str, str, str], int] = {}
    for row in old_rows:
        add_ticket_to_delta(
            delta,
            date_text,
            row["net_weight"],
            held=row["ticket_no"] in held_numbers,
            sign=-1,
        )

    conn.execute("DELETE FROM tickets WHERE slip_date = ?", (date_text,))
    new_count = 0
    batch: list[tuple[str, int, str]] = []
    for ticket_no, weight in new_rows:
        add_ticket_to_delta(
            delta,
            date_text,
            weight,
            held=ticket_no in held_numbers,
            sign=+1,
        )
        batch.append((date_text, ticket_no, weight))
        new_count += 1
        if len(batch) >= 500:
            conn.executemany(
                "INSERT INTO tickets(slip_date, ticket_no, net_weight) VALUES (?, ?, ?)",
                batch,
            )
            batch.clear()
    if batch:
        conn.executemany(
            "INSERT INTO tickets(slip_date, ticket_no, net_weight) VALUES (?, ?, ?)",
            batch,
        )
    changed_keys = apply_delta(conn, delta)
    revision = _bump_revision(conn, date_text)

    return {
        "date": date_text,
        "old_count": len(old_rows),
        "new_count": new_count,
        "subtotal_delta_keys": changed_keys,
        "revision": revision,
    }


def replace_day_tickets(
    conn: sqlite3.Connection, slip_date: str, tickets: list[tuple[str, int]]
) -> dict[str, int]:
    day = parse_iso_date(slip_date, "上传日期")
    date_text = day.isoformat()
    with write_transaction(conn):
        return _replace_day_rows(conn, date_text, tickets)


def _stream_pool_tickets(conn: sqlite3.Connection, token: str, encoding: str):
    """Yield (ticket_no, net_weight) from the on-disk pool, one row at a time."""
    cursor = _pool_rows(conn, token, encoding)
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield row["ticket_no"], int(row["net_weight"])


# ---------------------------------------------------------------------------
# Pending dual-encoding uploads
# ---------------------------------------------------------------------------


def create_pending_upload(
    conn: sqlite3.Connection,
    slip_date: str,
    detection: dict[str, object],
    filename: Optional[str] = None,
    ttl_seconds: int = PENDING_TTL_SECONDS,
) -> dict[str, object]:
    """Persist a detected-but-unconfirmed upload; the old day keeps serving."""
    day = parse_iso_date(slip_date, "上传日期").isoformat()
    token = detection.get("token") or secrets.token_urlsafe(24)
    detection["token"] = token
    now = time.time()
    candidates = [
        c.summary() if isinstance(c, Candidate) else dict(c)
        for c in detection["candidates"]
    ]
    divergence = detection.get("divergence") or {}
    with write_transaction(conn):
        base_revision = _day_revision(conn, day)
        conn.execute(
            """
            INSERT INTO pending_uploads(
                token, slip_date, base_revision, filename, file_size,
                raw_bytes_read, bom, candidates_json, divergence_json,
                created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                day,
                base_revision,
                filename,
                int(detection.get("file_size") or 0),
                int(detection.get("raw_bytes_read") or 0),
                1 if detection.get("bom") else 0,
                json.dumps(candidates, ensure_ascii=False),
                json.dumps(divergence, ensure_ascii=False),
                now,
                now + ttl_seconds,
            ),
        )
    return {
        "token": token,
        "date": day,
        "base_revision": base_revision,
        "expires_at": now + ttl_seconds,
        "filename": filename,
        "candidates": candidates,
        "divergence": divergence,
    }


def get_pending_upload(conn: sqlite3.Connection, token: str) -> Optional[dict[str, object]]:
    row = conn.execute(
        "SELECT * FROM pending_uploads WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    return {
        "token": row["token"],
        "date": row["slip_date"],
        "base_revision": int(row["base_revision"]),
        "filename": row["filename"],
        "file_size": int(row["file_size"]),
        "raw_bytes_read": int(row["raw_bytes_read"]),
        "bom": bool(row["bom"]),
        "candidates": json.loads(row["candidates_json"] or "[]"),
        "divergence": json.loads(row["divergence_json"] or "{}"),
        "created_at": float(row["created_at"]),
        "expires_at": float(row["expires_at"]),
        "expired": float(row["expires_at"]) <= time.time(),
    }


class ConfirmationExpired(ValidationError):
    """The pending choice expired, or the day was re-uploaded since."""


class DayAlreadyReplaced(ValidationError):
    """The same day was already replaced; the reviewer must re-upload."""


def confirm_pending_upload(
    conn: sqlite3.Connection, token: str, encoding: str
) -> dict[str, object]:
    """Replace the day using the chosen encoding, or explain why it cannot.

    On expiry, a stale base revision, a write failure, or an invalid choice,
    nothing changes: original tickets, holds, and ranks remain as before.
    """
    if encoding not in CANDIDATE_ENCODINGS:
        raise ValidationError("请选择 UTF-8 或 GB18030")

    pending = get_pending_upload(conn, token)
    if pending is None:
        raise ConfirmationExpired("待确认已过期或不存在，请重新上传")

    if pending["expired"]:
        with write_transaction(conn):
            _delete_pool(conn, token)
            conn.execute("DELETE FROM pending_uploads WHERE token = ?", (token,))
        raise ConfirmationExpired("待确认已过期，请重新上传")

    day = pending["date"]
    if pending["bom"] and encoding != ENCODING_UTF8:
        raise ValidationError("该文件带 UTF-8 BOM，只能按 UTF-8 读取")

    chosen = next(
        (c for c in pending["candidates"] if c.get("encoding") == encoding), None
    )
    if chosen is None or not chosen.get("ok"):
        label = "UTF-8" if encoding == ENCODING_UTF8 else "GB18030"
        raise ValidationError(f"该文件按 {label} 无法入库，请选择另一种编码")

    expected_count = int(chosen.get("ticket_count") or 0)
    pooled = conn.execute(
        "SELECT count(*) FROM pending_rows WHERE token = ? AND encoding = ?",
        (token, encoding),
    ).fetchone()[0]
    if pooled != expected_count:
        raise ConfirmationExpired("待确认数据不完整，请重新上传")

    try:
        with write_transaction(conn):
            current_revision = _day_revision(conn, day)
            if current_revision != pending["base_revision"]:
                raise DayAlreadyReplaced(
                    "同日日报已被换过，这份待确认不再适用，请重新上传"
                )
            summary = _replace_day_rows(
                conn, day, _stream_pool_tickets(conn, token, encoding)
            )
            if summary["new_count"] != expected_count:
                raise sqlite3.IntegrityError(
                    f"待确认行数不符：期望 {expected_count}，实际 {summary['new_count']}"
                )
            _delete_pool(conn, token)
            conn.execute("DELETE FROM pending_uploads WHERE token = ?", (token,))
    except ValidationError:
        raise
    except Exception as exc:
        # Includes sqlite3.Error and any injected/unexpected failure: the write
        # transaction has rolled back, so the previous committed state stands.
        if isinstance(exc, sqlite3.Error):
            detail = str(exc)
        else:
            detail = f"{type(exc).__name__}: {exc}"
        raise ValidationError(
            "换报写入失败，已回滚；原单、暂扣和名次保持原样", errors=[detail]
        ) from exc

    summary["encoding"] = encoding
    summary["filename"] = pending["filename"]
    return summary


def discard_pending_upload(conn: sqlite3.Connection, token: str) -> None:
    """Drop a pending record and its row pool; the day itself is untouched."""
    with write_transaction(conn):
        _delete_pool(conn, token)
        conn.execute("DELETE FROM pending_uploads WHERE token = ?", (token,))


def replace_day_from_pool(
    conn: sqlite3.Connection,
    slip_date: str,
    token: str,
    encoding: str,
) -> dict[str, object]:
    """Directly replace a day from an already-validated on-disk row pool.

    Used when only one encoding imports, or both produce identical details, so
    no reviewer choice is needed. Holds committed before this transaction are
    applied to the new tickets and both subtotal calibers.
    """
    day = parse_iso_date(slip_date, "上传日期").isoformat()
    try:
        with write_transaction(conn):
            summary = _replace_day_rows(
                conn, day, _stream_pool_tickets(conn, token, encoding)
            )
            _delete_pool(conn, token)
    except sqlite3.Error as exc:
        raise ValidationError(
            "换报写入失败，已回滚；原单、暂扣和名次保持原样", errors=[str(exc)]
        ) from exc
    except Exception as exc:
        raise ValidationError(
            "换报写入失败，已回滚；原单、暂扣和名次保持原样",
            errors=[f"{type(exc).__name__}: {exc}"],
        ) from exc
    summary["encoding"] = encoding
    return summary


def _get_ticket_row(conn: sqlite3.Connection, day: str, ticket_no: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT slip_date, ticket_no, net_weight
        FROM tickets
        WHERE slip_date = ? AND ticket_no = ?
        """,
        (day, ticket_no),
    ).fetchone()


def hold_ticket(
    conn: sqlite3.Connection,
    slip_date: str,
    ticket_no: str,
    reason: Optional[str] = None,
) -> dict[str, object]:
    day = parse_iso_date(slip_date, "单据日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    with write_transaction(conn):
        existing_hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (day, ticket_no),
        ).fetchone()
        ticket = _get_ticket_row(conn, day, ticket_no)

        if existing_hold:
            action = "already_held"
            if reason is not None:
                conn.execute(
                    "UPDATE holds SET reason = ? WHERE slip_date = ? AND ticket_no = ?",
                    (reason.strip() or None, day, ticket_no),
                )
        else:
            action = "held"
            conn.execute(
                "INSERT INTO holds(slip_date, ticket_no, reason) VALUES (?, ?, ?)",
                (day, ticket_no, (reason or "").strip() or None),
            )
            if ticket is not None:
                adjust_existing_ticket_active(conn, day, ticket["net_weight"], -1)

        hold = conn.execute(
            "SELECT slip_date, ticket_no, reason, created_at FROM holds WHERE slip_date=? AND ticket_no=?",
            (day, ticket_no),
        ).fetchone()

    return {
        "action": action,
        "hold": dict(hold) if hold else None,
        "ticket": dict(ticket) if ticket else None,
        "original_missing": ticket is None,
    }


def release_ticket(conn: sqlite3.Connection, slip_date: str, ticket_no: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "单据日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    with write_transaction(conn):
        existing_hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (day, ticket_no),
        ).fetchone()
        ticket = _get_ticket_row(conn, day, ticket_no)

        if existing_hold:
            action = "released"
            conn.execute(
                "DELETE FROM holds WHERE slip_date = ? AND ticket_no = ?",
                (day, ticket_no),
            )
            if ticket is not None:
                adjust_existing_ticket_active(conn, day, ticket["net_weight"], +1)
        else:
            action = "not_held"

    return {
        "action": action,
        "ticket": dict(ticket) if ticket else None,
        "original_missing": ticket is None,
    }


def get_day_readback(conn: sqlite3.Connection, slip_date: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "日期").isoformat()
    tickets = conn.execute(
        """
        SELECT t.slip_date, t.ticket_no, t.net_weight,
               CASE WHEN h.ticket_no IS NULL THEN 0 ELSE 1 END AS held,
               h.reason
        FROM tickets AS t
        LEFT JOIN holds AS h
          ON h.slip_date = t.slip_date AND h.ticket_no = t.ticket_no
        WHERE t.slip_date = ?
        ORDER BY t.ticket_no
        """,
        (day,),
    ).fetchall()
    missing_holds = conn.execute(
        """
        SELECT h.slip_date, h.ticket_no, h.reason, h.created_at
        FROM holds AS h
        LEFT JOIN tickets AS t
          ON t.slip_date = h.slip_date AND t.ticket_no = h.ticket_no
        WHERE h.slip_date = ? AND t.ticket_no IS NULL
        ORDER BY h.ticket_no
        """,
        (day,),
    ).fetchall()
    return {
        "date": day,
        "tickets": [dict(row) for row in tickets],
        "missing_holds": [dict(row) for row in missing_holds],
    }


def get_ticket_readback(conn: sqlite3.Connection, slip_date: str, ticket_no: str) -> dict[str, object]:
    day = parse_iso_date(slip_date, "日期").isoformat()
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")
    with read_transaction(conn):
        ticket = _get_ticket_row(conn, day, ticket_no)
        hold = conn.execute(
            "SELECT slip_date, ticket_no, reason, created_at FROM holds WHERE slip_date=? AND ticket_no=?",
            (day, ticket_no),
        ).fetchone()
    return {
        "ticket": dict(ticket) if ticket else None,
        "hold": dict(hold) if hold else None,
        "held": hold is not None,
    }


# ---------------------------------------------------------------------------
# Rank query and counted subtotal access
# ---------------------------------------------------------------------------


@dataclass
class KeyMeter:
    accessed: int = 0
    empty: int = 0
    details: list[dict[str, object]] = field(default_factory=list)

    def get(
        self,
        conn: sqlite3.Connection,
        date_seg: DateSeg,
        weight_seg: WeightSeg,
        scope: str,
    ) -> int:
        self.accessed += 1
        row = conn.execute(
            """
            SELECT ticket_count
            FROM subtotals
            WHERE date_key = ? AND weight_key = ? AND scope = ?
            """,
            (date_seg.key, weight_seg.key, scope),
        ).fetchone()
        is_empty = row is None
        if is_empty:
            self.empty += 1
            value = 0
        else:
            value = int(row["ticket_count"])
        self.details.append(
            {
                "n": self.accessed,
                "scope": scope,
                "date_segment": date_seg.label,
                "date_key": date_seg.key,
                "weight_band": weight_seg.label,
                "weight_key": weight_seg.key,
                "count": value,
                "empty": is_empty,
            }
        )
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "accessed": self.accessed,
            "empty": self.empty,
            "limit": SUBTOTAL_KEY_LIMIT,
            "within_limit": self.accessed <= SUBTOTAL_KEY_LIMIT,
            "details": self.details,
        }


def _rank_payload(total: int, higher: int, equal: int) -> dict[str, object]:
    if equal <= 0:
        return {
            "total": total,
            "higher": higher,
            "equal": equal,
            "rank_start": None,
            "rank_end": None,
            "rank_text": "无法计算",
        }
    start_rank = higher + 1
    end_rank = higher + equal
    if start_rank == end_rank:
        rank_text = f"第 {start_rank} 名"
    else:
        rank_text = f"第 {start_rank}-{end_rank} 名（{equal} 单同重）"
    return {
        "total": total,
        "higher": higher,
        "equal": equal,
        "rank_start": start_rank,
        "rank_end": end_rank,
        "rank_text": rank_text,
    }


def query_rank(
    conn: sqlite3.Connection,
    slip_date: str,
    ticket_no: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, object]:
    target_day = parse_iso_date(slip_date, "单据日期")
    ticket_no = (ticket_no or "").strip()
    if not ticket_no:
        raise ValidationError("单号不能为空")

    start = parse_iso_date(start_date, "起始日") if start_date else date(target_day.year, 1, 1)
    end = parse_iso_date(end_date, "结束日") if end_date else date(target_day.year, 12, 31)

    if start.year != end.year:
        raise ValidationError("起止日必须在同一年")
    if not (start <= target_day <= end):
        raise ValidationError("单据日期必须包含在起止区间内（含首尾）")
    if start.year != target_day.year:
        raise ValidationError("起止日与单据日期必须在同一年")

    date_segs = date_cover(start, end)
    root_weight = WeightSeg(0, WEIGHT_SIZE)
    greater_weights: Optional[list[WeightSeg]] = None
    equal_weight: Optional[WeightSeg] = None
    meter = KeyMeter()

    with read_transaction(conn):
        target = _get_ticket_row(conn, target_day.isoformat(), ticket_no)
        hold = conn.execute(
            "SELECT 1 FROM holds WHERE slip_date = ? AND ticket_no = ?",
            (target_day.isoformat(), ticket_no),
        ).fetchone()
        held = hold is not None

        if target is None:
            return {
                "found": False,
                "held": held,
                "query": {
                    "date": target_day.isoformat(),
                    "ticket_no": ticket_no,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                "date_segments": [seg.label for seg in date_segs],
                "date_segment_count": len(date_segs),
                "meter": meter.to_dict(),
                "all": None,
                "active": {
                    "held": held,
                    "rank_text": "暂扣中，名次留空" if held else "原单不存在，无法排名",
                },
            }

        weight = int(target["net_weight"])
        greater_weights = weight_greater_cover(weight)
        equal_weight = WeightSeg(weight, weight + 1)

        all_total = all_higher = all_equal = 0
        active_total = active_higher = active_equal = 0

        for ds in date_segs:
            all_total += meter.get(conn, ds, root_weight, SCOPE_ALL)
            for ws in greater_weights:
                all_higher += meter.get(conn, ds, ws, SCOPE_ALL)
            all_equal += meter.get(conn, ds, equal_weight, SCOPE_ALL)

            active_total += meter.get(conn, ds, root_weight, SCOPE_ACTIVE)
            if not held:
                for ws in greater_weights:
                    active_higher += meter.get(conn, ds, ws, SCOPE_ACTIVE)
                active_equal += meter.get(conn, ds, equal_weight, SCOPE_ACTIVE)

    all_result = _rank_payload(all_total, all_higher, all_equal)
    active_result: dict[str, object]
    if held:
        active_result = {
            "total": active_total,
            "higher": None,
            "equal": None,
            "rank_start": None,
            "rank_end": None,
            "rank_text": "",
            "held": True,
            "note": "目标单已暂扣；未暂扣口径只显示总数，名次按要求留空。",
        }
    else:
        active_result = _rank_payload(active_total, active_higher, active_equal)
        active_result["held"] = False

    return {
        "found": True,
        "held": held,
        "target": dict(target),
        "query": {
            "date": target_day.isoformat(),
            "ticket_no": ticket_no,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "weight": weight,
        },
        "date_segments": [seg.label for seg in date_segs],
        "date_segment_count": len(date_segs),
        "weight_greater_key_count": len(greater_weights),
        "meter": meter.to_dict(),
        "all": all_result,
        "active": active_result,
    }
