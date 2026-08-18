"""日報アプリのスモークテスト。

    python -m unittest discover -s tests

DATABASE_URL に一時 SQLite を指定してからアプリを読み込む。
"""
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-pass"

from urllib.parse import quote_plus  # noqa: E402

from cryptography.fernet import Fernet  # noqa: E402

import app as app_module  # noqa: E402
import calendar_routes  # noqa: E402
import google_calendar as gcal  # noqa: E402
from models import (  # noqa: E402
    DailyReport,
    GoogleCalendarLink,
    ReportCalendarEvent,
    ReportComment,
    User,
    db,
    today_jst,
)


class ReportAppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            app_module.bootstrap_admin(self.app)
        self.client = self.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.unlink(_DB_PATH)

    # --- ヘルパー ----------------------------------------------------
    def register(self, username, password="pass1234"):
        return self.client.post(
            "/register",
            data={"username": username, "password": password, "confirm": password},
            follow_redirects=True,
        )

    def login(self, username, password="pass1234", admin=False):
        return self.client.post(
            "/admin/login" if admin else "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def logout(self):
        return self.client.get("/logout", follow_redirects=True)

    def post_report(self, date_str, content="今日の作業", action="submit", **extra):
        data = {
            "report_date": date_str,
            "work_content": content,
            "work_hours": extra.get("work_hours", "8"),
            "tomorrow_plan": extra.get("tomorrow_plan", "明日の予定"),
            "issues": extra.get("issues", ""),
            "action": action,
        }
        return self.client.post("/reports/new", data=data, follow_redirects=True)

    # --- テスト ------------------------------------------------------
    def test_login_required_redirects(self):
        res = self.client.get("/reports", follow_redirects=True)
        self.assertIn("利用者ログイン", res.get_data(as_text=True))

    def test_create_submit_and_edit_report(self):
        self.register("taro")
        self.login("taro")
        today = today_jst().strftime("%Y-%m-%d")

        res = self.post_report(today, "テスト業務", action="draft")
        self.assertIn("下書き保存", res.get_data(as_text=True))
        with self.app.app_context():
            report = DailyReport.query.one()
            self.assertEqual(report.status, "draft")
            self.assertIsNone(report.submitted_at)
            self.assertEqual(report.work_hours, 8.0)
            report_id = report.id

        res = self.client.post(f"/reports/{report_id}/submit", follow_redirects=True)
        self.assertIn("提出しました", res.get_data(as_text=True))
        with self.app.app_context():
            report = db.session.get(DailyReport, report_id)
            self.assertEqual(report.status, "submitted")
            self.assertIsNotNone(report.submitted_at)

        res = self.client.post(
            f"/reports/{report_id}/edit",
            data={
                "report_date": today,
                "work_content": "修正後の業務",
                "work_hours": "7.5",
                "tomorrow_plan": "",
                "issues": "特になし",
                "action": "submit",
            },
            follow_redirects=True,
        )
        self.assertIn("修正後の業務", res.get_data(as_text=True))
        with self.app.app_context():
            report = db.session.get(DailyReport, report_id)
            self.assertEqual(report.work_hours, 7.5)
            self.assertEqual(report.issues, "特になし")

    def test_duplicate_date_is_rejected(self):
        self.register("taro")
        self.login("taro")
        today = today_jst().strftime("%Y-%m-%d")
        self.post_report(today)
        res = self.post_report(today, "二重投稿")
        self.assertIn("既にあります", res.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(DailyReport.query.count(), 1)

    def test_validation_errors(self):
        self.register("taro")
        self.login("taro")
        today = today_jst().strftime("%Y-%m-%d")

        res = self.post_report(today, "")
        self.assertIn("業務内容を入力してください", res.get_data(as_text=True))

        res = self.post_report(today, "作業", work_hours="30")
        self.assertIn("0〜24", res.get_data(as_text=True))

        res = self.post_report(today, "作業", work_hours="abc")
        self.assertIn("数値で入力してください", res.get_data(as_text=True))

        future = (today_jst() + timedelta(days=5)).strftime("%Y-%m-%d")
        res = self.post_report(future, "未来の作業")
        self.assertIn("未来すぎます", res.get_data(as_text=True))

        res = self.post_report("not-a-date", "作業")
        self.assertIn("日付を正しく入力してください", res.get_data(as_text=True))

        with self.app.app_context():
            self.assertEqual(DailyReport.query.count(), 0)

    def test_other_user_cannot_read_or_edit(self):
        self.register("taro")
        self.login("taro")
        self.post_report(today_jst().strftime("%Y-%m-%d"))
        with self.app.app_context():
            report_id = DailyReport.query.one().id
        self.logout()

        self.register("hanako")
        self.login("hanako")
        self.assertEqual(self.client.get(f"/reports/{report_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/reports/{report_id}/edit").status_code, 403)
        self.assertEqual(self.client.post(f"/reports/{report_id}/delete").status_code, 403)
        self.assertEqual(self.client.get("/admin/reports").status_code, 302)

    def test_admin_can_read_but_not_edit(self):
        self.register("taro")
        self.login("taro")
        self.post_report(today_jst().strftime("%Y-%m-%d"), "本人の業務")
        with self.app.app_context():
            report_id = DailyReport.query.one().id
        self.logout()

        self.login("admin", "admin-pass", admin=True)
        res = self.client.get(f"/reports/{report_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("本人の業務", res.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/reports/{report_id}/edit").status_code, 403)

    def test_comments(self):
        self.register("taro")
        self.login("taro")
        self.post_report(today_jst().strftime("%Y-%m-%d"))
        with self.app.app_context():
            report_id = DailyReport.query.one().id
        self.logout()

        self.login("admin", "admin-pass", admin=True)
        res = self.client.post(
            f"/reports/{report_id}/comments", data={"body": "お疲れさまです"}, follow_redirects=True
        )
        self.assertIn("お疲れさまです", res.get_data(as_text=True))
        with self.app.app_context():
            comment_id = ReportComment.query.one().id

        res = self.client.post(
            f"/reports/{report_id}/comments", data={"body": "   "}, follow_redirects=True
        )
        self.assertIn("コメントを入力してください", res.get_data(as_text=True))

        self.client.post(
            f"/reports/{report_id}/comments/{comment_id}/delete", follow_redirects=True
        )
        with self.app.app_context():
            self.assertEqual(ReportComment.query.count(), 0)

    def test_admin_list_filter_and_csv(self):
        self.register("taro")
        self.login("taro")
        today = today_jst()
        self.post_report(today.strftime("%Y-%m-%d"), "タロウの業務")
        self.logout()
        self.register("hanako")
        self.login("hanako")
        self.post_report(today.strftime("%Y-%m-%d"), "ハナコの業務")
        self.logout()

        self.login("admin", "admin-pass", admin=True)
        res = self.client.get("/admin/reports")
        body = res.get_data(as_text=True)
        self.assertIn("タロウの業務", body)
        self.assertIn("ハナコの業務", body)

        res = self.client.get("/admin/reports?q=ハナコ")
        body = res.get_data(as_text=True)
        self.assertIn("ハナコの業務", body)
        self.assertNotIn("タロウの業務", body)

        with self.app.app_context():
            taro_id = User.query.filter_by(username="taro").one().id
        res = self.client.get(f"/admin/reports?user_id={taro_id}")
        self.assertNotIn("ハナコの業務", res.get_data(as_text=True))

        res = self.client.get("/admin/reports?from=2000-01-01&to=2000-01-02")
        self.assertIn("該当する日報がありません", res.get_data(as_text=True))

        res = self.client.get("/admin/reports.csv")
        self.assertEqual(res.status_code, 200)
        csv_text = res.get_data(as_text=True)
        self.assertIn("業務内容", csv_text)
        self.assertIn("タロウの業務", csv_text)
        self.assertIn("attachment", res.headers["Content-Disposition"])

    def test_dashboard_shows_today_status(self):
        self.register("taro")
        self.login("taro")
        res = self.client.get("/dashboard")
        self.assertIn("本日の日報はまだ作成されていません", res.get_data(as_text=True))

        self.post_report(today_jst().strftime("%Y-%m-%d"))
        res = self.client.get("/dashboard")
        self.assertIn("提出済み", res.get_data(as_text=True))

        self.logout()
        self.login("admin", "admin-pass", admin=True)
        res = self.client.get("/dashboard")
        self.assertIn("本日の提出状況", res.get_data(as_text=True))

    def test_deleting_user_removes_reports(self):
        self.register("taro")
        self.login("taro")
        self.post_report(today_jst().strftime("%Y-%m-%d"))
        with self.app.app_context():
            report_id = DailyReport.query.one().id
        self.client.post(f"/reports/{report_id}/comments", data={"body": "自分メモ"})
        self.logout()

        self.login("admin", "admin-pass", admin=True)
        with self.app.app_context():
            taro_id = User.query.filter_by(username="taro").one().id
        self.client.post(f"/admin/users/{taro_id}/delete", follow_redirects=True)
        with self.app.app_context():
            self.assertEqual(DailyReport.query.count(), 0)
            self.assertEqual(ReportComment.query.count(), 0)

    def test_report_list_pagination_and_month_filter(self):
        self.register("taro")
        self.login("taro")
        today = today_jst()
        with self.app.app_context():
            user = User.query.filter_by(username="taro").one()
            for i in range(25):
                db.session.add(
                    DailyReport(
                        user_id=user.id,
                        report_date=today - timedelta(days=i),
                        work_content=f"業務 {i}",
                        status="submitted",
                    )
                )
            db.session.commit()

        res = self.client.get("/reports")
        self.assertIn("自分の日報（25 件）", res.get_data(as_text=True))
        res = self.client.get("/reports?page=2")
        self.assertIn("2 / 2", res.get_data(as_text=True))
        res = self.client.get(f"/reports?month={today.strftime('%Y-%m')}")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()


class BootstrapAdminTestCase(unittest.TestCase):
    """初期管理者のブートストラップと /setup 画面のテスト。"""

    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "SETUP_TOKEN")
        }
        for key in self._saved_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def admin_count(self):
        with self.app.app_context():
            return User.query.filter_by(role="admin").count()

    def test_no_admin_created_without_env(self):
        with self.app.app_context():
            self.assertFalse(app_module.bootstrap_admin(self.app))
        self.assertEqual(self.admin_count(), 0)
        # 管理者不在なら / は初期セットアップへ誘導する
        res = self.client.get("/", follow_redirects=True)
        self.assertIn("初期セットアップ", res.get_data(as_text=True))

    def test_bootstrap_from_env_runs_once(self):
        os.environ["ADMIN_USERNAME"] = "boss"
        os.environ["ADMIN_PASSWORD"] = "boss-password"
        with self.app.app_context():
            self.assertTrue(app_module.bootstrap_admin(self.app))
            # 2 回目以降は何もしない
            self.assertFalse(app_module.bootstrap_admin(self.app))
        self.assertEqual(self.admin_count(), 1)

        res = self.client.post(
            "/admin/login",
            data={"username": "boss", "password": "boss-password"},
            follow_redirects=True,
        )
        self.assertIn("管理者としてログインしました", res.get_data(as_text=True))

    def test_bootstrap_skips_when_username_taken(self):
        with self.app.app_context():
            user = User(username="boss")
            user.set_password("pass1234")
            db.session.add(user)
            db.session.commit()
        os.environ["ADMIN_USERNAME"] = "boss"
        os.environ["ADMIN_PASSWORD"] = "boss-password"
        with self.app.app_context():
            self.assertFalse(app_module.bootstrap_admin(self.app))
        self.assertEqual(self.admin_count(), 0)

    def test_setup_creates_admin_and_logs_in(self):
        res = self.client.post(
            "/setup",
            data={"username": "boss", "password": "boss-password", "confirm": "boss-password"},
            follow_redirects=True,
        )
        body = res.get_data(as_text=True)
        self.assertIn("管理者「boss」を作成しました", body)
        self.assertIn("本日の提出状況", body)  # 管理者としてログイン済み
        self.assertEqual(self.admin_count(), 1)

        # 2 回目以降は使えない
        res = self.client.get("/setup", follow_redirects=True)
        self.assertIn("初期セットアップは使用できません", res.get_data(as_text=True))

    def test_setup_validations(self):
        cases = [
            ({"username": "", "password": "boss-password", "confirm": "boss-password"},
             "ユーザー名とパスワードを入力してください"),
            ({"username": "boss", "password": "short", "confirm": "short"},
             "8 文字以上"),
            ({"username": "boss", "password": "boss-password", "confirm": "different"},
             "パスワードが一致しません"),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                res = self.client.post("/setup", data=data, follow_redirects=True)
                self.assertIn(message, res.get_data(as_text=True))
        self.assertEqual(self.admin_count(), 0)

    def test_setup_requires_token_when_configured(self):
        os.environ["SETUP_TOKEN"] = "secret-token"
        data = {"username": "boss", "password": "boss-password", "confirm": "boss-password"}

        res = self.client.post("/setup", data=dict(data, token="wrong"), follow_redirects=True)
        self.assertIn("合言葉が正しくありません", res.get_data(as_text=True))
        self.assertEqual(self.admin_count(), 0)

        res = self.client.post(
            "/setup", data=dict(data, token="secret-token"), follow_redirects=True
        )
        self.assertIn("管理者「boss」を作成しました", res.get_data(as_text=True))
        self.assertEqual(self.admin_count(), 1)


class AdminMypageTestCase(unittest.TestCase):
    """管理者マイページ（ログインID・パスワード変更）のテスト。"""

    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            app_module.bootstrap_admin(self.app)
        self.client = self.app.test_client()
        self.login_admin()

    def login_admin(self, password="admin-pass"):
        return self.client.post(
            "/admin/login",
            data={"username": "admin", "password": password},
            follow_redirects=True,
        )

    def post_mypage(self, **data):
        return self.client.post("/admin/mypage", data=data, follow_redirects=True)

    def test_requires_admin(self):
        self.client.get("/logout")
        self.client.post(
            "/register",
            data={"username": "taro", "password": "pass1234", "confirm": "pass1234"},
        )
        self.client.post("/login", data={"username": "taro", "password": "pass1234"})
        res = self.client.get("/admin/mypage")
        self.assertEqual(res.status_code, 302)
        res = self.client.get("/admin/mypage", follow_redirects=True)
        self.assertIn("管理者権限が必要です", res.get_data(as_text=True))

    def test_shows_account_info(self):
        res = self.client.get("/admin/mypage")
        body = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn("マイページ", body)
        self.assertIn("admin", body)
        self.assertIn("管理者の人数", body)

    def test_old_settings_url_redirects(self):
        res = self.client.get("/admin/settings")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].endswith("/admin/mypage"))

    def test_change_username(self):
        res = self.post_mypage(action="username", username="boss", current_password="admin-pass")
        self.assertIn("ログインIDを「boss」に変更しました", res.get_data(as_text=True))

        self.client.get("/logout")
        res = self.client.post(
            "/admin/login",
            data={"username": "boss", "password": "admin-pass"},
            follow_redirects=True,
        )
        self.assertIn("管理者としてログインしました", res.get_data(as_text=True))

    def test_change_username_rejects_bad_input(self):
        with self.app.app_context():
            other = User(username="taro")
            other.set_password("pass1234")
            db.session.add(other)
            db.session.commit()

        cases = [
            (dict(username="boss", current_password="wrong"), "現在のパスワードが正しくありません"),
            (dict(username="", current_password="admin-pass"), "ログインIDを入力してください"),
            (dict(username="admin", current_password="admin-pass"), "ログインIDが変わっていません"),
            (dict(username="taro", current_password="admin-pass"), "既に使われています"),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                res = self.post_mypage(action="username", **data)
                self.assertIn(message, res.get_data(as_text=True))

        with self.app.app_context():
            self.assertIsNotNone(User.query.filter_by(username="admin").first())

    def test_change_password(self):
        res = self.post_mypage(
            action="password",
            new_password="new-password",
            confirm="new-password",
            current_password="admin-pass",
        )
        self.assertIn("パスワードを変更しました", res.get_data(as_text=True))
        # 変更後もログインは維持される
        self.assertEqual(self.client.get("/admin/mypage").status_code, 200)

        self.client.get("/logout")
        self.assertIn("正しくありません", self.login_admin("admin-pass").get_data(as_text=True))
        self.assertIn("管理者としてログインしました", self.login_admin("new-password").get_data(as_text=True))

    def test_change_password_rejects_bad_input(self):
        cases = [
            (dict(new_password="new-password", confirm="new-password", current_password="wrong"),
             "現在のパスワードが正しくありません"),
            (dict(new_password="short", confirm="short", current_password="admin-pass"),
             "8 文字以上"),
            (dict(new_password="new-password", confirm="different", current_password="admin-pass"),
             "一致しません"),
            (dict(new_password="admin-pass", confirm="admin-pass", current_password="admin-pass"),
             "現在と同じパスワードです"),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                res = self.post_mypage(action="password", **data)
                self.assertIn(message, res.get_data(as_text=True))

        self.client.get("/logout")
        self.assertIn("管理者としてログインしました", self.login_admin().get_data(as_text=True))


class FakeGoogle:
    """Google の OAuth / Calendar API を置き換えるテスト用ダブル。"""

    def __init__(self):
        self.token_requests = []
        self.api_calls = []
        self.events = {}
        self.items = []
        self.next_id = 1
        self.scope = (
            "https://www.googleapis.com/auth/calendar.events "
            "https://www.googleapis.com/auth/userinfo.email"
        )
        self.refresh_token = "refresh-token-1"
        self.fail_write = False

    def post_form(self, url, data):
        self.token_requests.append((url, data))
        if url == gcal.REVOKE_ENDPOINT:
            return {}
        if data.get("grant_type") == "authorization_code":
            return {
                "access_token": "access-1",
                "refresh_token": self.refresh_token,
                "scope": self.scope,
                "expires_in": 3599,
            }
        return {"access_token": "access-1", "expires_in": 3599}

    def api(self, method, url, access_token, params=None, json=None):
        self.api_calls.append((method, url, params, json))
        if url == gcal.USERINFO_ENDPOINT:
            return {"email": "taro@example.com"}
        if method == "GET" and url.endswith("/events"):
            return {"items": self.items}
        if method == "POST" and url.endswith("/events"):
            if self.fail_write:
                raise gcal.CalendarError("書き込みに失敗しました")
            event_id = f"evt-{self.next_id}"
            self.next_id += 1
            self.events[event_id] = json
            return {"id": event_id}
        if method == "PATCH":
            event_id = url.rsplit("/", 1)[1]
            if event_id not in self.events:
                raise gcal.CalendarError("予定が見つかりません")
            self.events[event_id] = json
            return {"id": event_id}
        if method == "DELETE":
            self.events.pop(url.rsplit("/", 1)[1], None)
            return {}
        return {}


class GoogleCalendarTestCase(unittest.TestCase):
    """Google カレンダー連携（読み取りのみ / 書き込みも許可）のテスト。"""

    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            app_module.bootstrap_admin(self.app)
        self.client = self.app.test_client()
        self.fake = FakeGoogle()
        self._real = (gcal._post_form, gcal._api)
        gcal._post_form, gcal._api = self.fake.post_form, self.fake.api
        os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"
        os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

        self.client.post(
            "/register",
            data={"username": "taro", "password": "pass1234", "confirm": "pass1234"},
        )
        self.client.post("/login", data={"username": "taro", "password": "pass1234"})

    def tearDown(self):
        gcal._post_form, gcal._api = self._real
        for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY"):
            os.environ.pop(key, None)

    # --- ヘルパー ----------------------------------------------------
    def connect(self, scope=None):
        """同意画面を通ったことにして連携を完了させる。"""
        if scope is not None:
            self.fake.scope = scope
        with self.client.session_transaction() as sess:
            sess[calendar_routes.STATE_KEY] = "state-1"
        return self.client.get(
            "/calendar/callback?code=auth-code&state=state-1", follow_redirects=True
        )

    def link(self):
        with self.app.app_context():
            return (
                GoogleCalendarLink.query.join(User)
                .filter(User.username == "taro")
                .first()
            )

    def create_report(self, action="submit", content="テスト業務"):
        self.client.post(
            "/reports/new",
            data={
                "report_date": today_jst().strftime("%Y-%m-%d"),
                "work_content": content,
                "work_hours": "8",
                "tomorrow_plan": "",
                "issues": "",
                "action": action,
            },
            follow_redirects=True,
        )
        with self.app.app_context():
            return DailyReport.query.one().id

    # --- 設定・認可 --------------------------------------------------
    def test_disabled_when_not_configured(self):
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        res = self.client.get("/calendar")
        self.assertIn("まだ利用できません", res.get_data(as_text=True))

        res = self.client.get("/calendar/connect?mode=readonly", follow_redirects=True)
        self.assertIn("Google 連携が未設定です", res.get_data(as_text=True))

    def test_connect_uses_scope_for_selected_mode(self):
        res = self.client.get("/calendar/connect?mode=readonly")
        self.assertEqual(res.status_code, 302)
        location = res.headers["Location"]
        self.assertIn("accounts.google.com", location)
        self.assertIn(quote_plus("https://www.googleapis.com/auth/calendar.readonly"), location)
        self.assertNotIn(quote_plus("https://www.googleapis.com/auth/calendar.events"), location)
        self.assertIn("access_type=offline", location)

        res = self.client.get("/calendar/connect?mode=write")
        location = res.headers["Location"]
        self.assertIn(quote_plus("https://www.googleapis.com/auth/calendar.events"), location)

    def test_connect_rejects_unknown_mode(self):
        res = self.client.get("/calendar/connect?mode=everything", follow_redirects=True)
        self.assertIn("権限を選択してください", res.get_data(as_text=True))

    def test_callback_requires_matching_state(self):
        with self.client.session_transaction() as sess:
            sess[calendar_routes.STATE_KEY] = "state-1"
        res = self.client.get(
            "/calendar/callback?code=auth-code&state=tampered", follow_redirects=True
        )
        self.assertIn("セッションが一致しません", res.get_data(as_text=True))
        self.assertIsNone(self.link())

    def test_callback_stores_encrypted_refresh_token(self):
        res = self.connect()
        self.assertIn("Google カレンダーと連携しました", res.get_data(as_text=True))
        link = self.link()
        self.assertEqual(link.mode, "write")
        self.assertEqual(link.google_email, "taro@example.com")
        # 生のトークンがそのまま保存されていないこと
        self.assertNotIn("refresh-token-1", link.refresh_token_encrypted)
        self.assertEqual(gcal.decrypt_token(link.refresh_token_encrypted), "refresh-token-1")

    def test_granted_scope_decides_mode(self):
        """書き込みを要求しても、実際に許可された範囲を保存する。"""
        self.connect(scope="https://www.googleapis.com/auth/calendar.readonly")
        self.assertEqual(self.link().mode, "readonly")

    def test_callback_without_calendar_scope_fails(self):
        res = self.connect(scope="https://www.googleapis.com/auth/userinfo.email")
        self.assertIn("アクセスが許可されませんでした", res.get_data(as_text=True))
        self.assertIsNone(self.link())

    def test_disconnect_revokes_and_removes(self):
        self.connect()
        res = self.client.post("/calendar/disconnect", follow_redirects=True)
        self.assertIn("連携を解除しました", res.get_data(as_text=True))
        self.assertIsNone(self.link())
        self.assertIn(gcal.REVOKE_ENDPOINT, [url for url, _ in self.fake.token_requests])

    # --- 予定の取り込み ----------------------------------------------
    def test_import_events_into_form(self):
        self.connect(scope="https://www.googleapis.com/auth/calendar.readonly")
        self.fake.items = [
            {
                "summary": "定例MTG",
                "start": {"dateTime": "2026-08-19T09:00:00+09:00"},
                "end": {"dateTime": "2026-08-19T10:00:00+09:00"},
            },
            {"summary": "全社イベント", "start": {"date": "2026-08-19"}, "end": {"date": "2026-08-20"}},
            {
                "summary": "断った打ち合わせ",
                "start": {"dateTime": "2026-08-19T13:00:00+09:00"},
                "end": {"dateTime": "2026-08-19T14:00:00+09:00"},
                "attendees": [{"self": True, "responseStatus": "declined"}],
            },
        ]
        res = self.client.get("/reports/new?from_calendar=1")
        body = res.get_data(as_text=True)
        self.assertIn("09:00-10:00 定例MTG", body)
        self.assertIn("終日 全社イベント", body)
        self.assertNotIn("断った打ち合わせ", body)  # 辞退した予定は取り込まない
        self.assertIn("予定を 2 件取り込みました", body)

    def test_import_without_link_asks_to_connect(self):
        res = self.client.get("/reports/new?from_calendar=1", follow_redirects=True)
        self.assertIn("先に Google カレンダーと連携してください", res.get_data(as_text=True))

    # --- 日報の書き出し ----------------------------------------------
    def test_submit_writes_event_when_write_allowed(self):
        self.connect()
        report_id = self.create_report()
        self.assertEqual(len(self.fake.events), 1)
        body = list(self.fake.events.values())[0]
        self.assertIn("日報: taro", body["summary"])
        self.assertIn("テスト業務", body["description"])
        self.assertEqual(body["start"]["date"], today_jst().strftime("%Y-%m-%d"))
        with self.app.app_context():
            self.assertEqual(ReportCalendarEvent.query.filter_by(report_id=report_id).count(), 1)

    def test_readonly_link_never_writes(self):
        self.connect(scope="https://www.googleapis.com/auth/calendar.readonly")
        self.create_report()
        self.assertEqual(self.fake.events, {})
        with self.app.app_context():
            self.assertEqual(ReportCalendarEvent.query.count(), 0)

    def test_draft_is_not_written(self):
        self.connect()
        self.create_report(action="draft")
        self.assertEqual(self.fake.events, {})

    def test_resubmit_updates_the_same_event(self):
        self.connect()
        report_id = self.create_report()
        self.client.post(
            f"/reports/{report_id}/edit",
            data={
                "report_date": today_jst().strftime("%Y-%m-%d"),
                "work_content": "修正後の業務",
                "work_hours": "8",
                "tomorrow_plan": "",
                "issues": "",
                "action": "submit",
            },
            follow_redirects=True,
        )
        self.assertEqual(len(self.fake.events), 1)  # 増えていない
        self.assertIn("修正後の業務", list(self.fake.events.values())[0]["description"])

    def test_unsubmit_and_delete_remove_the_event(self):
        self.connect()
        report_id = self.create_report()
        self.client.post(f"/reports/{report_id}/submit", follow_redirects=True)  # 下書きへ
        self.assertEqual(self.fake.events, {})
        with self.app.app_context():
            self.assertEqual(ReportCalendarEvent.query.count(), 0)

        self.client.post(f"/reports/{report_id}/submit", follow_redirects=True)  # 再提出
        self.assertEqual(len(self.fake.events), 1)
        self.client.post(f"/reports/{report_id}/delete", follow_redirects=True)
        self.assertEqual(self.fake.events, {})
        with self.app.app_context():
            self.assertEqual(ReportCalendarEvent.query.count(), 0)

    def test_report_is_saved_even_if_calendar_write_fails(self):
        self.connect()
        self.fake.fail_write = True
        res = self.client.post(
            "/reports/new",
            data={
                "report_date": today_jst().strftime("%Y-%m-%d"),
                "work_content": "書き出し失敗のテスト",
                "work_hours": "8",
                "tomorrow_plan": "",
                "issues": "",
                "action": "submit",
            },
            follow_redirects=True,
        )
        body = res.get_data(as_text=True)
        self.assertIn("日報を提出しました", body)
        self.assertIn("カレンダーへの書き出しに失敗しました", body)
        with self.app.app_context():
            self.assertEqual(DailyReport.query.count(), 1)
