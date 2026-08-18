"""日報（作成・一覧・詳細・コメント・管理者向け集計/CSV）のルーティング。"""
import csv
import io
from datetime import date, datetime, timedelta

from flask import (
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

import google_calendar as gcal
from auth import admin_required, current_user, login_required
from models import (
    STATUS_DRAFT,
    STATUS_SUBMITTED,
    STATUSES,
    DailyReport,
    ReportComment,
    User,
    db,
    now_jst,
    today_jst,
)

PER_PAGE = 20


# ---------------------------------------------------------------------------
# 入力値のパース
# ---------------------------------------------------------------------------
def parse_date(value: str):
    """'YYYY-MM-DD' を date に変換する。不正な値なら None。"""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_month(value: str):
    """'YYYY-MM' を (月初, 月末) に変換する。不正な値なら None。"""
    try:
        first = datetime.strptime((value or "").strip(), "%Y-%m").date()
    except ValueError:
        return None
    if first.month == 12:
        next_first = date(first.year + 1, 1, 1)
    else:
        next_first = date(first.year, first.month + 1, 1)
    return first, next_first - timedelta(days=1)


def parse_hours(value: str):
    """作業時間（0〜24 の小数）に変換する。空なら None、不正なら ValueError。"""
    text = (value or "").strip()
    if not text:
        return None
    hours = float(text)  # 呼び出し側で ValueError を捕捉する
    if hours < 0 or hours > 24:
        raise ValueError("作業時間は 0〜24 の範囲で入力してください。")
    return round(hours, 2)


def parse_page(value: str) -> int:
    try:
        page = int(value or 1)
    except ValueError:
        return 1
    return max(page, 1)


# ---------------------------------------------------------------------------
# 検索・ページング
# ---------------------------------------------------------------------------
def _apply_filters(query, args):
    """クエリ文字列（ユーザー・期間・ステータス・キーワード）で絞り込む。"""
    user_id = (args.get("user_id") or "").strip()
    if user_id.isdigit():
        query = query.filter(DailyReport.user_id == int(user_id))

    month = parse_month(args.get("month", ""))
    if month:
        query = query.filter(DailyReport.report_date.between(*month))
    else:
        date_from = parse_date(args.get("from", ""))
        date_to = parse_date(args.get("to", ""))
        if date_from:
            query = query.filter(DailyReport.report_date >= date_from)
        if date_to:
            query = query.filter(DailyReport.report_date <= date_to)

    status = (args.get("status") or "").strip()
    if status in STATUSES:
        query = query.filter(DailyReport.status == status)

    keyword = (args.get("q") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            db.or_(
                DailyReport.work_content.ilike(like),
                DailyReport.tomorrow_plan.ilike(like),
                DailyReport.issues.ilike(like),
            )
        )
    return query


def _paginate(query, page: int):
    """(件数, 該当ページの一覧, 最終ページ番号) を返す。"""
    total = query.count()
    last_page = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(page, last_page)
    items = query.limit(PER_PAGE).offset((page - 1) * PER_PAGE).all()
    return total, items, page, last_page


# ---------------------------------------------------------------------------
# ダッシュボード用の集計（app.py から利用）
# ---------------------------------------------------------------------------
def personal_summary(user):
    """本人向けサマリ: 今日の日報・今月の提出数・直近の日報。"""
    today = today_jst()
    month_start = today.replace(day=1)
    return {
        "today": today,
        "today_report": DailyReport.query.filter_by(
            user_id=user.id, report_date=today
        ).first(),
        "submitted_this_month": DailyReport.query.filter(
            DailyReport.user_id == user.id,
            DailyReport.status == STATUS_SUBMITTED,
            DailyReport.report_date >= month_start,
            DailyReport.report_date <= today,
        ).count(),
        "draft_count": DailyReport.query.filter_by(
            user_id=user.id, status=STATUS_DRAFT
        ).count(),
        "recent": DailyReport.query.filter_by(user_id=user.id)
        .order_by(DailyReport.report_date.desc())
        .limit(5)
        .all(),
    }


def admin_summary():
    """管理者向けサマリ: 今日の提出状況と未提出者、最近提出された日報。"""
    today = today_jst()
    submitted_ids = {
        row[0]
        for row in db.session.query(DailyReport.user_id)
        .filter(
            DailyReport.report_date == today,
            DailyReport.status == STATUS_SUBMITTED,
        )
        .all()
    }
    users = User.query.order_by(User.username.asc()).all()
    return {
        "today": today,
        "user_count": len(users),
        "submitted_count": len(submitted_ids),
        "not_submitted": [u for u in users if u.id not in submitted_ids],
        "recent": DailyReport.query.filter_by(status=STATUS_SUBMITTED)
        .order_by(DailyReport.submitted_at.desc().nullslast())
        .limit(5)
        .all(),
    }


# ---------------------------------------------------------------------------
# Google カレンダー連携
# ---------------------------------------------------------------------------
def _import_calendar_text(user, day, existing: str) -> str:
    """その日の予定を取り込み、業務内容のテキストに足して返す。"""
    link = user.calendar_link
    if link is None:
        flash("先に Google カレンダーと連携してください。", "error")
        return existing

    try:
        events = gcal.list_events(link, day)
    except gcal.CalendarError as exc:
        flash(str(exc), "error")
        return existing

    if not events:
        flash(f"{day:%Y-%m-%d} の予定は見つかりませんでした。", "error")
        return existing

    flash(f"{day:%Y-%m-%d} の予定を {len(events)} 件取り込みました。", "success")
    text = gcal.format_events(events)
    return f"{existing}\n{text}".strip() if existing.strip() else text


def _sync_calendar(report) -> None:
    """提出済みの日報をカレンダーへ書き出す（書き込み権限があるときだけ）。

    日報自体は保存済みなので、失敗しても警告を出すだけにする。
    """
    try:
        if gcal.push_report(report):
            flash("Google カレンダーにも書き出しました。", "success")
    except gcal.CalendarError as exc:
        flash(f"カレンダーへの書き出しに失敗しました: {exc}", "error")


def _unsync_calendar(report) -> None:
    """書き出した予定を消す。"""
    try:
        gcal.remove_report(report)
    except gcal.CalendarError as exc:
        flash(f"カレンダーの予定を削除できませんでした: {exc}", "error")


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
def register_report_routes(app) -> None:
    @app.route("/reports")
    @login_required
    def reports():
        """自分の日報一覧。"""
        user = current_user()
        query = _apply_filters(
            DailyReport.query.filter_by(user_id=user.id), request.args
        ).order_by(DailyReport.report_date.desc())
        total, items, page, last_page = _paginate(query, parse_page(request.args.get("page")))
        return render_template(
            "reports.html",
            reports=items,
            total=total,
            page=page,
            last_page=last_page,
            statuses=STATUSES,
            today=today_jst(),
        )

    @app.route("/reports/new", methods=["GET", "POST"])
    @login_required
    def report_new():
        """日報の新規作成。同じ日付の日報が既にあれば編集画面へ誘導する。"""
        user = current_user()
        default_date = parse_date(request.args.get("date", "")) or today_jst()

        if request.method == "POST":
            form = _read_form(request.form)
            error = _validate(form)
            if error:
                flash(error, "error")
                return render_template("report_form.html", report=None, form=form, mode="new")

            existing = DailyReport.query.filter_by(
                user_id=user.id, report_date=form["report_date"]
            ).first()
            if existing is not None:
                flash(
                    f"{form['report_date']:%Y-%m-%d} の日報は既にあります。こちらを編集してください。",
                    "error",
                )
                return redirect(url_for("report_edit", report_id=existing.id))

            report = DailyReport(user_id=user.id)
            _assign(report, form)
            db.session.add(report)
            db.session.commit()
            flash(_saved_message(report), "success")
            if report.is_submitted:
                _sync_calendar(report)
            return redirect(url_for("report_detail", report_id=report.id))

        form = {
            "report_date": default_date,
            "work_hours": "",
            "work_content": "",
            "tomorrow_plan": "",
            "issues": "",
            "submit": False,
        }
        if request.args.get("from_calendar"):
            form["work_content"] = _import_calendar_text(user, default_date, "")
        return render_template("report_form.html", report=None, form=form, mode="new")

    @app.route("/reports/<int:report_id>")
    @login_required
    def report_detail(report_id):
        """日報の詳細。本人と管理者のみ閲覧できる。"""
        report = _get_visible_report(report_id)
        return render_template("report_detail.html", report=report)

    @app.route("/reports/<int:report_id>/edit", methods=["GET", "POST"])
    @login_required
    def report_edit(report_id):
        """日報の編集。編集できるのは本人だけ。"""
        report = _get_own_report(report_id)

        if request.method == "POST":
            form = _read_form(request.form)
            error = _validate(form)
            if not error:
                duplicated = DailyReport.query.filter(
                    DailyReport.user_id == report.user_id,
                    DailyReport.report_date == form["report_date"],
                    DailyReport.id != report.id,
                ).first()
                if duplicated is not None:
                    error = f"{form['report_date']:%Y-%m-%d} の日報は既に別に存在します。"
            if error:
                flash(error, "error")
                return render_template("report_form.html", report=report, form=form, mode="edit")

            _assign(report, form)
            db.session.commit()
            flash(_saved_message(report), "success")
            if report.is_submitted:
                _sync_calendar(report)
            else:
                _unsync_calendar(report)
            return redirect(url_for("report_detail", report_id=report.id))

        form = {
            "report_date": report.report_date,
            "work_hours": "" if report.work_hours is None else report.work_hours,
            "work_content": report.work_content,
            "tomorrow_plan": report.tomorrow_plan,
            "issues": report.issues,
            "submit": report.is_submitted,
        }
        if request.args.get("from_calendar"):
            form["work_content"] = _import_calendar_text(
                current_user(), report.report_date, report.work_content
            )
        return render_template("report_form.html", report=report, form=form, mode="edit")

    @app.route("/reports/<int:report_id>/delete", methods=["POST"])
    @login_required
    def report_delete(report_id):
        report = _get_own_report(report_id)
        _unsync_calendar(report)
        db.session.delete(report)
        db.session.commit()
        flash("日報を削除しました。", "success")
        return redirect(url_for("reports"))

    @app.route("/reports/<int:report_id>/submit", methods=["POST"])
    @login_required
    def report_submit(report_id):
        """下書きを提出済みにする / 提出済みを下書きに戻す。"""
        report = _get_own_report(report_id)
        if report.is_submitted:
            report.status = STATUS_DRAFT
            report.submitted_at = None
            message = "日報を下書きに戻しました。"
        else:
            report.status = STATUS_SUBMITTED
            report.submitted_at = now_jst()
            message = "日報を提出しました。"
        db.session.commit()
        flash(message, "success")
        if report.is_submitted:
            _sync_calendar(report)
        else:
            _unsync_calendar(report)
        return redirect(url_for("report_detail", report_id=report.id))

    @app.route("/reports/<int:report_id>/comments", methods=["POST"])
    @login_required
    def report_comment(report_id):
        """日報へコメントする。本人と管理者のみ。"""
        report = _get_visible_report(report_id)
        body = (request.form.get("body") or "").strip()
        if not body:
            flash("コメントを入力してください。", "error")
        else:
            db.session.add(
                ReportComment(report_id=report.id, user_id=current_user().id, body=body)
            )
            db.session.commit()
            flash("コメントを投稿しました。", "success")
        return redirect(url_for("report_detail", report_id=report.id))

    @app.route("/reports/<int:report_id>/comments/<int:comment_id>/delete", methods=["POST"])
    @login_required
    def report_comment_delete(report_id, comment_id):
        """コメントの削除。投稿者本人と管理者のみ。"""
        report = _get_visible_report(report_id)
        comment = db.session.get(ReportComment, comment_id)
        if comment is None or comment.report_id != report.id:
            abort(404)
        user = current_user()
        if comment.user_id != user.id and not user.is_admin:
            abort(403)
        db.session.delete(comment)
        db.session.commit()
        flash("コメントを削除しました。", "success")
        return redirect(url_for("report_detail", report_id=report.id))

    # --- 管理者専用 ------------------------------------------------------
    @app.route("/admin/reports")
    @admin_required
    def admin_reports():
        """全ユーザーの日報一覧（ユーザー・期間・ステータス・キーワードで絞り込み）。"""
        query = _apply_filters(DailyReport.query, request.args).order_by(
            DailyReport.report_date.desc(), DailyReport.user_id.asc()
        )
        total, items, page, last_page = _paginate(query, parse_page(request.args.get("page")))
        return render_template(
            "admin_reports.html",
            reports=items,
            total=total,
            page=page,
            last_page=last_page,
            users=User.query.order_by(User.username.asc()).all(),
            statuses=STATUSES,
            today=today_jst(),
        )

    @app.route("/admin/reports.csv")
    @admin_required
    def admin_reports_csv():
        """絞り込み結果を CSV でダウンロードする（Excel 向けに BOM 付き UTF-8）。"""
        query = _apply_filters(DailyReport.query, request.args).order_by(
            DailyReport.report_date.asc(), DailyReport.user_id.asc()
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["日付", "ユーザー名", "ステータス", "作業時間", "業務内容", "明日の予定", "課題・所感", "提出日時"]
        )
        for report in query.all():
            writer.writerow(
                [
                    report.report_date.strftime("%Y-%m-%d"),
                    report.user.username if report.user else "",
                    report.status_label,
                    "" if report.work_hours is None else report.work_hours,
                    report.work_content,
                    report.tomorrow_plan,
                    report.issues,
                    report.submitted_at.strftime("%Y-%m-%d %H:%M") if report.submitted_at else "",
                ]
            )
        filename = f"reports_{today_jst():%Y%m%d}.csv"
        return Response(
            "﻿" + buffer.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------
def _get_visible_report(report_id: int) -> DailyReport:
    """本人か管理者だけが閲覧できる日報を取得する。"""
    report = db.session.get(DailyReport, report_id)
    if report is None:
        abort(404)
    user = current_user()
    if report.user_id != user.id and not user.is_admin:
        abort(403)
    return report


def _get_own_report(report_id: int) -> DailyReport:
    """本人だけが編集できる日報を取得する（管理者でも他人の日報は編集不可）。"""
    report = db.session.get(DailyReport, report_id)
    if report is None:
        abort(404)
    if report.user_id != current_user().id:
        abort(403)
    return report


def _read_form(form) -> dict:
    return {
        "report_date": (form.get("report_date") or "").strip(),
        "work_hours": (form.get("work_hours") or "").strip(),
        "work_content": (form.get("work_content") or "").strip(),
        "tomorrow_plan": (form.get("tomorrow_plan") or "").strip(),
        "issues": (form.get("issues") or "").strip(),
        "submit": form.get("action") == "submit",
    }


def _validate(form: dict):
    """フォームを検証し、問題があればエラーメッセージを返す。値は正規化する。"""
    report_date = parse_date(form["report_date"])
    if report_date is None:
        return "日付を正しく入力してください。"
    if report_date > today_jst() + timedelta(days=1):
        return "日付が未来すぎます。翌日までの日報のみ登録できます。"
    if not form["work_content"]:
        return "業務内容を入力してください。"
    try:
        form["work_hours"] = parse_hours(form["work_hours"])
    except ValueError as exc:
        return str(exc) if str(exc).startswith("作業時間") else "作業時間は数値で入力してください。"
    form["report_date"] = report_date
    return None


def _assign(report: DailyReport, form: dict) -> None:
    report.report_date = form["report_date"]
    report.work_hours = form["work_hours"]
    report.work_content = form["work_content"]
    report.tomorrow_plan = form["tomorrow_plan"]
    report.issues = form["issues"]
    if form["submit"]:
        if not report.is_submitted:
            report.submitted_at = now_jst()
        report.status = STATUS_SUBMITTED
    else:
        report.status = STATUS_DRAFT
        report.submitted_at = None


def _saved_message(report: DailyReport) -> str:
    return "日報を提出しました。" if report.is_submitted else "日報を下書き保存しました。"
