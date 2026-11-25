"""Flask review application for disputed scale tickets."""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import core


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-review-secret")
    if db_path is None:
        db_path = os.environ.get("DB_PATH", str(Path(__file__).with_name("data") / "scale.db"))
    if db_path == ":memory:":
        # A normal file-backed temporary DB preserves one schema across the
        # short-lived per-request connections used by Flask/SQLite here.
        fd, db_path = tempfile.mkstemp(prefix="scale-memory-", suffix=".db")
        os.close(fd)
    app.config["DB_PATH"] = db_path
    core.init_db_path(db_path)

    @app.template_global()
    def encoding_label(encoding: str) -> str:
        return "UTF-8" if encoding == core.ENCODING_UTF8 else "GB18030"

    def get_db():
        if "db" not in g:
            g.db = core.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(exc: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    def first_value(*names: str, default: str = "") -> str:
        source: dict[str, Any] = {}
        if request.is_json:
            source = request.get_json(silent=True) or {}
        for name in names:
            if name in request.values and str(request.values.get(name) or "").strip():
                return str(request.values.get(name)).strip()
            if name in source and source[name] not in (None, ""):
                return str(source[name]).strip()
        return default

    def first_uploaded_file():
        for name in ("file", "csv", "upload", "csv_file"):
            if name in request.files:
                return request.files[name]
        if request.files:
            return next(iter(request.files.values()))
        return None

    def wants_json() -> bool:
        if request.path.startswith("/api/"):
            return True
        accept = request.accept_mimetypes
        return accept.best == "application/json" and accept["application/json"] > accept["text/html"]

    def error_response(message: str, status: int = 400, errors: list[str] | None = None):
        if wants_json():
            return jsonify(ok=False, error=message, errors=errors or []), status
        for line in errors or [message]:
            flash(line, "error")
        return redirect(request.referrer or url_for("index"), code=303)

    @app.get("/")
    def index():
        day = request.args.get("date", "").strip()
        day_data = None
        if day:
            try:
                day_data = core.get_day_readback(get_db(), day)
            except core.ValidationError as exc:
                flash(exc.message, "error")
        return render_template(
            "index.html",
            today=core.date.today().isoformat(),
            day_data=day_data,
        )

    @app.get("/day")
    def day_view():
        day = first_value("date", "slip_date", "day")
        try:
            day_data = core.get_day_readback(get_db(), day)
        except core.ValidationError as exc:
            return error_response(exc.message)
        return render_template("day.html", day_data=day_data, today=core.date.today().isoformat())

    @app.get("/ticket")
    def ticket_view():
        day = first_value("date", "slip_date", "ticket_date")
        ticket_no = first_value("ticket_no", "ticketNo", "ticket", "no", "id")
        try:
            data = core.get_ticket_readback(get_db(), day, ticket_no)
        except core.ValidationError as exc:
            return error_response(exc.message)
        return render_template(
            "ticket.html",
            data=data,
            date=day,
            ticket_no=ticket_no,
            today=core.date.today().isoformat(),
        )

    @app.post("/upload")
    def upload():
        day = first_value("date", "slip_date", "slipDate", "upload_date", "day")
        upload_file = first_uploaded_file()
        csv_text = first_value("csv_text", "text")
        pool_token: str | None = None

        try:
            if upload_file is not None and upload_file.filename:
                # Validate the date before touching the file so a bad date
                # cannot leave a parsed row pool behind.
                core.parse_iso_date(day, "上传日期")
                filename = upload_file.filename
                pool_token = secrets.token_urlsafe(24)
                spool = core.CountingSpool(upload_file.stream)
                try:
                    detection = core.detect_upload(get_db(), spool, pool_token)
                finally:
                    spool.close()
                if detection["requires_choice"]:
                    pending = core.create_pending_upload(
                        get_db(), day, detection, filename
                    )
                    pool_token = None  # now owned by the pending record
                    if wants_json():
                        return jsonify(
                            ok=True,
                            pending=True,
                            **to_jsonable(pending),
                        ), 409
                    return redirect(
                        url_for("pending_view", token=pending["token"]), code=303
                    )
                if not detection["chosen"]:
                    raise core.ValidationError("未能判别文件编码，请重新上传")
                summary = core.replace_day_from_pool(
                    get_db(), day, pool_token, detection["chosen"]
                )
                pool_token = None
                summary["filename"] = filename
                summary["bom"] = detection["bom"]
            elif csv_text:
                filename = "pasted.csv"
                tickets = core.parse_csv_text(csv_text)
                summary = core.replace_day_tickets(get_db(), day, tickets)
                summary["encoding"] = core.ENCODING_UTF8
                summary["filename"] = filename
                summary["bom"] = False
            else:
                raise core.ValidationError("请选择 CSV 文件，或粘贴 CSV 文本")
        except core.ValidationError as exc:
            if pool_token is not None:
                core.discard_pending_upload(get_db(), pool_token)
            return error_response(exc.message, errors=exc.errors)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            app.logger.exception("database failure during upload")
            if pool_token is not None:
                core.discard_pending_upload(get_db(), pool_token)
            return error_response(f"数据库写入失败，已回滚，旧数据仍可查询：{exc}", status=500)

        if wants_json():
            return jsonify(ok=True, pending=False, **summary)
        flash(
            f"已整日替换 {summary['date']}：旧 {summary['old_count']} 单，"
            f"新 {summary['new_count']} 单；按 {summary['encoding'].upper()} 读取；"
            f"暂扣记录保持有效。",
            "success",
        )
        return redirect(url_for("day_view", date=summary["date"], uploaded=1), code=303)

    @app.get("/pending/<token>")
    def pending_view(token: str):
        pending = core.get_pending_upload(get_db(), token)
        if pending is None:
            if wants_json():
                return jsonify(ok=False, error="待确认已过期或不存在，请重新上传"), 404
            flash("待确认已过期或不存在，请重新上传。", "error")
            return redirect(url_for("index"), code=303)
        if pending["expired"]:
            if wants_json():
                return jsonify(ok=False, error="待确认已过期，请重新上传"), 410
            flash("待确认已过期，请重新上传。", "error")
            return redirect(url_for("index"), code=303)
        current_revision = core.get_day_revision(get_db(), pending["date"])
        return render_template(
            "pending.html",
            pending=pending,
            day_data=core.get_day_readback(get_db(), pending["date"]),
            current_revision=current_revision,
            stale=current_revision != pending["base_revision"],
            expires_text=core.format_local_time(pending["expires_at"]),
            expires_in=max(0, int(pending["expires_at"] - core.time.time())),
            today=core.date.today().isoformat(),
        )

    @app.post("/confirm")
    def confirm():
        token = first_value("token")
        encoding = first_value("encoding", "charset")
        try:
            summary = core.confirm_pending_upload(get_db(), token, encoding)
        except core.DayAlreadyReplaced as exc:
            if wants_json():
                return jsonify(ok=False, error=exc.message, reupload=True), 409
            flash(exc.message, "error")
            return redirect(url_for("index"), code=303)
        except core.ConfirmationExpired as exc:
            if wants_json():
                return jsonify(ok=False, error=exc.message, expired=True), 410
            flash(exc.message, "error")
            return redirect(url_for("index"), code=303)
        except core.ValidationError as exc:
            return error_response(exc.message, errors=exc.errors)
        except sqlite3.Error as exc:  # pragma: no cover
            app.logger.exception("database failure during confirm")
            return error_response(f"换报写入失败，已回滚：{exc}", status=500)

        if wants_json():
            return jsonify(ok=True, **summary)
        flash(
            f"已按 {summary['encoding'].upper()} 换报 {summary['date']}："
            f"旧 {summary['old_count']} 单，新 {summary['new_count']} 单；"
            f"生效前已提交的暂扣已更新原单和两种名次小计。",
            "success",
        )
        return redirect(url_for("day_view", date=summary["date"], confirmed=1), code=303)

    @app.post("/pending/<token>/cancel")
    def cancel_pending(token: str):
        pending = core.get_pending_upload(get_db(), token)
        if pending is not None:
            core.discard_pending_upload(get_db(), token)
        if wants_json():
            return jsonify(ok=True)
        flash("已放弃本次换报，旧日报继续供数。", "info")
        return redirect(url_for("index"), code=303)

    @app.post("/api/confirm")
    def api_confirm_alias():
        return confirm()

    @app.post("/hold")
    def hold():
        day = first_value("date", "slip_date", "slipDate", "hold_date", "ticket_date")
        ticket_no = first_value("ticket_no", "ticketNo", "ticket", "no", "id")
        reason = first_value("reason", "note") or None
        try:
            result = core.hold_ticket(get_db(), day, ticket_no, reason)
        except core.ValidationError as exc:
            return error_response(exc.message)
        except sqlite3.Error as exc:  # pragma: no cover
            app.logger.exception("database failure during hold")
            return error_response(f"暂扣失败，已回滚：{exc}", status=500)

        if wants_json():
            return jsonify(ok=True, **result)
        if result["action"] == "already_held":
            flash("该单已处于暂扣状态。", "info")
        elif result["original_missing"]:
            flash("已暂扣；当前日期下没有原单，暂扣记录会保留到以后重报。", "warning")
        else:
            flash("已暂扣，未暂扣口径小计已同步更新。", "success")
        return redirect(url_for("ticket_view", date=day, ticket_no=ticket_no), code=303)

    @app.post("/release")
    def release():
        day = first_value("date", "slip_date", "slipDate", "release_date", "ticket_date")
        ticket_no = first_value("ticket_no", "ticketNo", "ticket", "no", "id")
        try:
            result = core.release_ticket(get_db(), day, ticket_no)
        except core.ValidationError as exc:
            return error_response(exc.message)
        except sqlite3.Error as exc:  # pragma: no cover
            app.logger.exception("database failure during release")
            return error_response(f"解除失败，已回滚：{exc}", status=500)

        if wants_json():
            return jsonify(ok=True, **result)
        if result["action"] == "not_held":
            flash("该单本来就没有暂扣记录。", "info")
        else:
            flash("已解除暂扣，未暂扣口径小计已同步恢复。", "success")
        return redirect(url_for("ticket_view", date=day, ticket_no=ticket_no), code=303)

    def perform_query():
        day = first_value("date", "slip_date", "slipDate", "ticket_date", "qdate")
        ticket_no = first_value("ticket_no", "ticketNo", "ticket", "no", "id")
        start = first_value(
            "start", "start_date", "startDate", "from_date", "begin"
        ) or None
        end = first_value("end", "end_date", "endDate", "to_date", "until") or None
        return core.query_rank(get_db(), day, ticket_no, start, end), {
            "date": day,
            "ticket_no": ticket_no,
            "start": start,
            "end": end,
        }

    @app.route("/query", methods=["GET", "POST"])
    def query_view():
        try:
            result, form = perform_query()
        except core.ValidationError as exc:
            if wants_json():
                return jsonify(ok=False, error=exc.message), 400
            flash(exc.message, "error")
            return redirect(url_for("index"), code=303)

        if wants_json():
            return jsonify(ok=True, result=to_jsonable(result))
        return render_template("query.html", result=result, form=form)

    @app.get("/api/rank")
    def api_rank():
        try:
            result, _ = perform_query()
        except core.ValidationError as exc:
            return jsonify(ok=False, error=exc.message), 400
        return jsonify(ok=True, result=to_jsonable(result))

    @app.route("/api/query", methods=["GET", "POST"])
    def api_query_alias():
        return api_rank()

    @app.post("/api/upload")
    def api_upload_alias():
        return upload()

    @app.post("/api/hold")
    def api_hold_alias():
        return hold()

    @app.post("/api/release")
    def api_release_alias():
        return release()

    @app.errorhandler(404)
    def not_found(_):
        if wants_json():
            return jsonify(ok=False, error="not found"), 404
        return render_template("index.html", today=core.date.today().isoformat(), day_data=None), 404

    return app


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
