"""Google カレンダー連携（OAuth 2.0 + Calendar API）。

権限は 2 段階から選べる。

- readonly: 予定の読み取りのみ（calendar.readonly）
- write:    予定の読み取りに加えて、日報をカレンダーへ書き出せる（calendar.events）

リフレッシュトークンは暗号化して保存する。鍵は TOKEN_ENCRYPTION_KEY、
未設定なら SECRET_KEY から導出する（SECRET_KEY を変えると再連携が必要）。
"""
import base64
import hashlib
import os
from datetime import datetime, timedelta

import requests

from models import (
    CALENDAR_MODE_READONLY,
    CALENDAR_MODE_WRITE,
    JST,
    GoogleCalendarLink,
    ReportCalendarEvent,
    db,
    now_jst,
)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"
API_BASE = "https://www.googleapis.com/calendar/v3"

# 権限ごとに要求するスコープ。write は読み取りも含む。
SCOPES = {
    CALENDAR_MODE_READONLY: (
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
    ),
    CALENDAR_MODE_WRITE: (
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/userinfo.email",
    ),
}

TIMEOUT = 15


class CalendarError(Exception):
    """連携中に起きた想定内のエラー（画面にそのまま出せるメッセージ）。"""


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
def client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()


def is_configured() -> bool:
    """OAuth クライアントの設定が揃っているか。"""
    return bool(client_id() and client_secret())


def redirect_uri(default: str) -> str:
    """GOOGLE_REDIRECT_URI があればそれを使う（リバースプロキシ配下の対策）。"""
    return (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip() or default


# ---------------------------------------------------------------------------
# トークンの暗号化
# ---------------------------------------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet  # 遅延 import（未使用時の起動を軽くする）

    key = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if not key:
        secret = os.environ.get("SECRET_KEY", "dev-secret-change-me")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode()
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CalendarError(
            "TOKEN_ENCRYPTION_KEY が不正です。Fernet 形式の鍵を設定してください。"
        ) from exc


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CalendarError(
            "保存済みトークンを復号できませんでした。連携を解除して、もう一度接続してください。"
        ) from exc


# ---------------------------------------------------------------------------
# HTTP（テストではこの 2 つを差し替える）
# ---------------------------------------------------------------------------
def _post_form(url: str, data: dict) -> dict:
    response = requests.post(url, data=data, timeout=TIMEOUT)
    if response.status_code >= 400:
        raise CalendarError(f"Google への要求が失敗しました（{response.status_code}）: {response.text[:200]}")
    return response.json() if response.content else {}


def _api(method: str, url: str, access_token: str, params=None, json=None) -> dict:
    response = requests.request(
        method,
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        json=json,
        timeout=TIMEOUT,
    )
    if response.status_code == 404:
        raise CalendarError("カレンダーまたは予定が見つかりませんでした。")
    if response.status_code >= 400:
        raise CalendarError(
            f"カレンダー API の呼び出しに失敗しました（{response.status_code}）: {response.text[:200]}"
        )
    return response.json() if response.content else {}


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
def build_auth_url(mode: str, callback_url: str, state: str) -> str:
    """同意画面の URL を作る。mode に応じて要求スコープが変わる。"""
    if not is_configured():
        raise CalendarError("Google 連携が未設定です。GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を設定してください。")
    if mode not in SCOPES:
        raise CalendarError("不正な権限が指定されました。")

    from urllib.parse import urlencode

    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(callback_url),
        "response_type": "code",
        "scope": " ".join(SCOPES[mode]),
        "access_type": "offline",  # リフレッシュトークンを受け取る
        "prompt": "consent",  # 権限を変えたときに確実に再同意させる
        "include_granted_scopes": "false",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(code: str, callback_url: str) -> dict:
    """認可コードをトークンに交換する。"""
    return _post_form(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect_uri(callback_url),
            "grant_type": "authorization_code",
        },
    )


def refresh_access_token(refresh_token: str) -> str:
    """リフレッシュトークンからアクセストークンを取り直す。"""
    payload = _post_form(
        TOKEN_ENDPOINT,
        {
            "refresh_token": refresh_token,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "grant_type": "refresh_token",
        },
    )
    access_token = payload.get("access_token")
    if not access_token:
        raise CalendarError("アクセストークンを取得できませんでした。連携し直してください。")
    return access_token


def revoke(token: str) -> None:
    """Google 側の許可を取り消す（失敗しても致命的ではない）。"""
    try:
        _post_form(REVOKE_ENDPOINT, {"token": token})
    except CalendarError:
        pass


def fetch_email(access_token: str) -> str:
    try:
        return _api("GET", USERINFO_ENDPOINT, access_token).get("email", "") or ""
    except CalendarError:
        return ""


def mode_for_scope(granted_scope: str) -> str:
    """実際に許可されたスコープから権限を判定する。

    ユーザーが同意画面で書き込みのチェックを外した場合に、書き込み可能だと
    誤認しないようにするため、保存前に必ずこれで確認する。
    """
    scopes = set((granted_scope or "").split())
    if "https://www.googleapis.com/auth/calendar.events" in scopes:
        return CALENDAR_MODE_WRITE
    if "https://www.googleapis.com/auth/calendar.readonly" in scopes:
        return CALENDAR_MODE_READONLY
    raise CalendarError("カレンダーへのアクセスが許可されませんでした。")


def save_link(user, token_payload: dict) -> GoogleCalendarLink:
    """取得したトークンを保存（または更新）する。"""
    refresh_token = token_payload.get("refresh_token")
    access_token = token_payload.get("access_token", "")
    mode = mode_for_scope(token_payload.get("scope", ""))

    link = user.calendar_link
    if not refresh_token:
        if link is None:
            raise CalendarError(
                "リフレッシュトークンを取得できませんでした。Google アカウントの「サードパーティ アプリ」"
                "から本アプリのアクセス権を削除してから、もう一度お試しください。"
            )
        # 再同意でリフレッシュトークンが返らない場合は既存のものを使い続ける
        refresh_token = decrypt_token(link.refresh_token_encrypted)

    if link is None:
        link = GoogleCalendarLink(user_id=user.id)
        db.session.add(link)

    link.refresh_token_encrypted = encrypt_token(refresh_token)
    link.granted_scope = token_payload.get("scope", "")
    link.mode = mode
    link.google_email = fetch_email(access_token) if access_token else link.google_email
    link.updated_at = now_jst()
    db.session.commit()
    return link


def disconnect(link: GoogleCalendarLink) -> None:
    """連携を解除する（Google 側の許可も取り消す）。"""
    try:
        revoke(decrypt_token(link.refresh_token_encrypted))
    except CalendarError:
        pass
    db.session.delete(link)
    db.session.commit()


def access_token_for(link: GoogleCalendarLink) -> str:
    return refresh_access_token(decrypt_token(link.refresh_token_encrypted))


# ---------------------------------------------------------------------------
# 予定の読み取り
# ---------------------------------------------------------------------------
def list_events(link: GoogleCalendarLink, day) -> list:
    """指定日（日本時間）の予定を時刻順に返す。辞退済みの予定は除く。"""
    access_token = access_token_for(link)
    start = datetime.combine(day, datetime.min.time()).replace(tzinfo=JST)
    payload = _api(
        "GET",
        f"{API_BASE}/calendars/{link.calendar_id}/events",
        access_token,
        params={
            "timeMin": start.isoformat(),
            "timeMax": (start + timedelta(days=1)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 50,
        },
    )

    events = []
    for item in payload.get("items", []):
        if item.get("status") == "cancelled" or _declined(item):
            continue
        events.append(
            {
                "summary": item.get("summary") or "（無題の予定）",
                "start": item.get("start", {}),
                "end": item.get("end", {}),
                "all_day": "date" in item.get("start", {}),
            }
        )
    return events


def _declined(item: dict) -> bool:
    for attendee in item.get("attendees", []) or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def _hhmm(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(JST).strftime("%H:%M")
    except ValueError:
        return ""


def format_events(events: list) -> str:
    """予定を日報に貼り付けやすいテキストにする。"""
    lines = []
    for event in events:
        if event["all_day"]:
            lines.append(f"・終日 {event['summary']}")
            continue
        start = _hhmm(event["start"].get("dateTime", ""))
        end = _hhmm(event["end"].get("dateTime", ""))
        span = f"{start}-{end}" if start and end else start or ""
        lines.append(f"・{span} {event['summary']}".replace("・ ", "・"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 日報の書き出し（write 権限のときだけ）
# ---------------------------------------------------------------------------
def _event_body(report) -> dict:
    sections = [f"【業務内容】\n{report.work_content}"]
    if report.tomorrow_plan:
        sections.append(f"【明日の予定】\n{report.tomorrow_plan}")
    if report.issues:
        sections.append(f"【課題・所感】\n{report.issues}")
    if report.work_hours is not None:
        sections.append(f"【作業時間】{report.work_hours} 時間")

    day = report.report_date
    return {
        "summary": f"日報: {report.user.username if report.user else ''}".strip(),
        "description": "\n\n".join(sections),
        "start": {"date": day.strftime("%Y-%m-%d")},
        "end": {"date": (day + timedelta(days=1)).strftime("%Y-%m-%d")},
        "transparency": "transparent",  # 予定を埋めない（空き時間のまま）
        "reminders": {"useDefault": False},
        "extendedProperties": {"private": {"nippou_report_id": str(report.id)}},
    }


def push_report(report) -> bool:
    """提出済みの日報をカレンダーへ書き出す。書き込み権限が無ければ何もしない。

    連携していない・読み取り専用の場合は False を返す（エラーにはしない）。
    """
    link = report.user.calendar_link if report.user else None
    if link is None or not link.can_write:
        return False

    access_token = access_token_for(link)
    body = _event_body(report)
    record = report.calendar_event

    if record is not None:
        try:
            _api(
                "PATCH",
                f"{API_BASE}/calendars/{record.calendar_id}/events/{record.event_id}",
                access_token,
                json=body,
            )
            record.synced_at = now_jst()
            db.session.commit()
            return True
        except CalendarError:
            # 予定が手で消された場合などは作り直す
            db.session.delete(record)
            db.session.commit()

    created = _api(
        "POST",
        f"{API_BASE}/calendars/{link.calendar_id}/events",
        access_token,
        json=body,
    )
    event_id = created.get("id")
    if not event_id:
        raise CalendarError("カレンダーに予定を作成できませんでした。")

    db.session.add(
        ReportCalendarEvent(
            report_id=report.id, calendar_id=link.calendar_id, event_id=event_id
        )
    )
    db.session.commit()
    return True


def remove_report(report) -> bool:
    """書き出した予定を削除する（下書きに戻したとき・日報を消すとき）。"""
    record = report.calendar_event
    if record is None:
        return False

    link = report.user.calendar_link if report.user else None
    if link is not None and link.can_write:
        try:
            _api(
                "DELETE",
                f"{API_BASE}/calendars/{record.calendar_id}/events/{record.event_id}",
                access_token_for(link),
            )
        except CalendarError:
            pass  # 既に消えていても記録だけは消す

    db.session.delete(record)
    db.session.commit()
    return True
