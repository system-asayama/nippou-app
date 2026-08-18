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

import app as app_module  # noqa: E402
from models import DailyReport, ReportComment, User, db, today_jst  # noqa: E402


class ReportAppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            app_module._seed_admin()
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
