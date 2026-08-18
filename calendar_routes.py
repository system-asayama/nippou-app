"""Google カレンダー連携の画面とコールバック。"""
import hmac
import secrets

from flask import flash, redirect, render_template, request, session, url_for

import google_calendar as gcal
from auth import current_user, login_required
from models import CALENDAR_MODES, CALENDAR_MODE_LABELS, today_jst

STATE_KEY = "calendar_oauth_state"


def register_calendar_routes(app) -> None:
    @app.route("/calendar")
    @login_required
    def calendar_settings():
        """連携状態の確認と、権限を選んでの接続・解除。"""
        user = current_user()
        link = user.calendar_link

        # 連携済みなら動作確認を兼ねて今日の予定を出す（失敗しても画面は開く）
        events, error = [], None
        if link is not None:
            try:
                events = gcal.list_events(link, today_jst())
            except gcal.CalendarError as exc:
                error = str(exc)

        return render_template(
            "calendar.html",
            link=link,
            configured=gcal.is_configured(),
            modes=CALENDAR_MODES,
            mode_labels=CALENDAR_MODE_LABELS,
            events=events,
            events_error=error,
            today=today_jst(),
        )

    @app.route("/calendar/connect")
    @login_required
    def calendar_connect():
        """選んだ権限で Google の同意画面へ送る。"""
        mode = request.args.get("mode") or ""
        if mode not in CALENDAR_MODES:
            flash("権限を選択してください。", "error")
            return redirect(url_for("calendar_settings"))

        state = secrets.token_urlsafe(32)
        session[STATE_KEY] = state
        try:
            auth_url = gcal.build_auth_url(
                mode, url_for("calendar_callback", _external=True), state
            )
        except gcal.CalendarError as exc:
            flash(str(exc), "error")
            return redirect(url_for("calendar_settings"))
        return redirect(auth_url)

    @app.route("/calendar/callback")
    @login_required
    def calendar_callback():
        """Google からの戻り先。トークンを保存する。"""
        user = current_user()
        expected_state = session.pop(STATE_KEY, "")
        state = request.args.get("state") or ""
        error = request.args.get("error")

        if error:
            flash(f"連携をキャンセルしました（{error}）。", "error")
            return redirect(url_for("calendar_settings"))
        if not expected_state or not hmac.compare_digest(state, expected_state):
            flash("セッションが一致しません。お手数ですが最初からやり直してください。", "error")
            return redirect(url_for("calendar_settings"))

        code = request.args.get("code") or ""
        if not code:
            flash("認可コードを受け取れませんでした。", "error")
            return redirect(url_for("calendar_settings"))

        try:
            payload = gcal.exchange_code(code, url_for("calendar_callback", _external=True))
            link = gcal.save_link(user, payload)
        except gcal.CalendarError as exc:
            flash(str(exc), "error")
            return redirect(url_for("calendar_settings"))

        flash(f"Google カレンダーと連携しました（権限: {link.mode_label}）。", "success")
        return redirect(url_for("calendar_settings"))

    @app.route("/calendar/disconnect", methods=["POST"])
    @login_required
    def calendar_disconnect():
        link = current_user().calendar_link
        if link is None:
            flash("連携されていません。", "error")
        else:
            gcal.disconnect(link)
            flash("Google カレンダーの連携を解除しました。", "success")
        return redirect(url_for("calendar_settings"))
