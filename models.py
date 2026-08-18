"""データベースモデル定義。"""
from datetime import date, datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# 利用可能なロール（権限）
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

# 日報のステータス
STATUS_DRAFT = "draft"
STATUS_SUBMITTED = "submitted"
STATUSES = (STATUS_DRAFT, STATUS_SUBMITTED)
STATUS_LABELS = {STATUS_DRAFT: "下書き", STATUS_SUBMITTED: "提出済み"}

# Google カレンダー連携で許可する権限
CALENDAR_MODE_READONLY = "readonly"
CALENDAR_MODE_WRITE = "write"
CALENDAR_MODES = (CALENDAR_MODE_READONLY, CALENDAR_MODE_WRITE)
CALENDAR_MODE_LABELS = {
    CALENDAR_MODE_READONLY: "読み取りのみ",
    CALENDAR_MODE_WRITE: "読み取りと書き込み",
}

# 日報は日本時間の日付で扱う（サーバーの TZ に依存させない）
JST = timezone(timedelta(hours=9))


def now_jst() -> datetime:
    """現在時刻（日本時間、tz 情報なし）を返す。"""
    return datetime.now(JST).replace(tzinfo=None)


def today_jst() -> date:
    """今日の日付（日本時間）を返す。"""
    return now_jst().date()


class User(db.Model):
    """ログインユーザー。admin / user の2種類のロールを持つ。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_USER)
    created_at = db.Column(db.DateTime, default=now_jst, nullable=False)

    # ユーザーを削除したら、その人の日報とコメントも一緒に削除する
    reports = db.relationship(
        "DailyReport",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    comments = db.relationship(
        "ReportComment",
        back_populates="author",
        cascade="all, delete-orphan",
    )
    calendar_link = db.relationship(
        "GoogleCalendarLink",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<User {self.username} ({self.role})>"


class DailyReport(db.Model):
    """1 ユーザー・1 日につき 1 件の日報。"""

    __tablename__ = "daily_reports"
    __table_args__ = (
        db.UniqueConstraint("user_id", "report_date", name="uq_daily_reports_user_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_date = db.Column(db.Date, nullable=False, index=True)
    work_content = db.Column(db.Text, nullable=False, default="")
    work_hours = db.Column(db.Float)
    tomorrow_plan = db.Column(db.Text, nullable=False, default="")
    issues = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT)
    submitted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now_jst, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_jst, onupdate=now_jst, nullable=False)

    user = db.relationship("User", back_populates="reports")
    comments = db.relationship(
        "ReportComment",
        back_populates="report",
        cascade="all, delete-orphan",
        order_by="ReportComment.created_at",
    )
    calendar_event = db.relationship(
        "ReportCalendarEvent",
        back_populates="report",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def is_submitted(self) -> bool:
        return self.status == STATUS_SUBMITTED

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<DailyReport {self.report_date} user={self.user_id} {self.status}>"


class ReportComment(db.Model):
    """日報へのコメント（上司からのフィードバックや本人の補足）。"""

    __tablename__ = "report_comments"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(
        db.Integer,
        db.ForeignKey("daily_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now_jst, nullable=False)

    report = db.relationship("DailyReport", back_populates="comments")
    author = db.relationship("User", back_populates="comments")

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<ReportComment report={self.report_id} user={self.user_id}>"


class GoogleCalendarLink(db.Model):
    """ユーザーと Google カレンダーの連携情報。

    リフレッシュトークンは暗号化して保存する（google_calendar.encrypt_token）。
    mode が readonly なら予定の取り込みのみ、write なら日報の書き出しもできる。
    """

    __tablename__ = "google_calendar_links"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    google_email = db.Column(db.String(255))
    calendar_id = db.Column(db.String(255), nullable=False, default="primary")
    mode = db.Column(db.String(20), nullable=False, default=CALENDAR_MODE_READONLY)
    refresh_token_encrypted = db.Column(db.Text, nullable=False)
    granted_scope = db.Column(db.Text, nullable=False, default="")
    connected_at = db.Column(db.DateTime, default=now_jst, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_jst, onupdate=now_jst, nullable=False)

    user = db.relationship("User", back_populates="calendar_link")

    @property
    def can_write(self) -> bool:
        return self.mode == CALENDAR_MODE_WRITE

    @property
    def mode_label(self) -> str:
        return CALENDAR_MODE_LABELS.get(self.mode, self.mode)

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<GoogleCalendarLink user={self.user_id} {self.mode}>"


class ReportCalendarEvent(db.Model):
    """日報をカレンダーへ書き出したときのイベント ID。

    同じ日報を二重に登録せず、更新・削除できるようにするために持つ。
    """

    __tablename__ = "report_calendar_events"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(
        db.Integer,
        db.ForeignKey("daily_reports.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    calendar_id = db.Column(db.String(255), nullable=False, default="primary")
    event_id = db.Column(db.String(255), nullable=False)
    synced_at = db.Column(db.DateTime, default=now_jst, onupdate=now_jst, nullable=False)

    report = db.relationship("DailyReport", back_populates="calendar_event")

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<ReportCalendarEvent report={self.report_id} event={self.event_id}>"
