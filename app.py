"""日報アプリ。

- セッションベースの認証（admin / user のロール）
- 利用者は自分の日報を作成・編集・提出し、コメントをやり取りできる
- 管理者は全員の日報を閲覧・検索・CSV 出力し、ユーザー管理もできる
"""
import hmac
import os

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import admin_required, current_user, login_required
from models import (
    ROLE_ADMIN,
    ROLE_USER,
    ROLES,
    STATUS_SUBMITTED,
    DailyReport,
    ReportComment,
    User,
    db,
)
from calendar_routes import register_calendar_routes
from reports import admin_summary, personal_summary, register_report_routes


def _normalize_db_url(url: str) -> str:
    # SQLAlchemy は postgres:// を認識しないため postgresql:// に変換
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_db_url(database_url)
    else:
        # DATABASE_URL が無い場合はローカル SQLite にフォールバック
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()
        bootstrap_admin(app)

    _register_routes(app)
    register_report_routes(app)
    register_calendar_routes(app)
    return app


# ---------------------------------------------------------------------------
# 初期管理者のブートストラップ
# ---------------------------------------------------------------------------
def admin_exists() -> bool:
    """管理者が 1 人でも登録されているか。"""
    return User.query.filter_by(role=ROLE_ADMIN).first() is not None


def bootstrap_admin(app: Flask) -> bool:
    """管理者が 1 人も居ないときだけ初期管理者を作成する。

    ADMIN_USERNAME と ADMIN_PASSWORD の両方が設定されていればそれで作成する
    （自動デプロイ向け）。未設定なら何も作らず、初回のみ `/setup` 画面から
    管理者を作成できる状態にしておく。既に管理者が居る場合は常に何もしない
    ので、管理者を消しても弱いパスワードのアカウントが復活することはない。
    """
    if admin_exists():
        return False

    username = (os.environ.get("ADMIN_USERNAME") or "").strip()
    password = os.environ.get("ADMIN_PASSWORD") or ""
    if not username or not password:
        app.logger.warning(
            "管理者が未登録です。ADMIN_USERNAME / ADMIN_PASSWORD が未設定のため、"
            "/setup から初期管理者を作成してください。"
        )
        return False

    if User.query.filter_by(username=username).first() is not None:
        app.logger.warning(
            "ADMIN_USERNAME=%s は既に利用者として使われているため初期管理者を作成できません。"
            "/setup から別の名前で作成してください。",
            username,
        )
        return False

    admin = User(username=username, role=ROLE_ADMIN)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    app.logger.info("初期管理者 %s を作成しました。", username)
    return True


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    @app.context_processor
    def inject_user():
        return {"current_user": current_user(), "needs_setup": not admin_exists()}

    @app.route("/")
    def index():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        if not admin_exists():
            return redirect(url_for("setup"))
        return redirect(url_for("login"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """初回だけ使える管理者作成画面。管理者が既に居る場合は使えない。

        SETUP_TOKEN が設定されている場合は、その合言葉の入力を必須にする。
        """
        if admin_exists():
            flash("管理者は既に登録済みです。初期セットアップは使用できません。", "error")
            return redirect(url_for("admin_login"))

        expected_token = os.environ.get("SETUP_TOKEN") or ""

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""
            token = request.form.get("token") or ""

            if expected_token and not hmac.compare_digest(token, expected_token):
                flash("セットアップ用の合言葉が正しくありません。", "error")
            elif not username or not password:
                flash("ユーザー名とパスワードを入力してください。", "error")
            elif len(password) < 8:
                flash("パスワードは 8 文字以上にしてください。", "error")
            elif password != confirm:
                flash("パスワードが一致しません。", "error")
            elif User.query.filter_by(username=username).first() is not None:
                flash("そのユーザー名は既に使われています。", "error")
            else:
                admin = User(username=username, role=ROLE_ADMIN)
                admin.set_password(password)
                db.session.add(admin)
                db.session.commit()
                session.clear()
                session["user_id"] = admin.id
                flash(f"管理者「{username}」を作成しました。", "success")
                return redirect(url_for("dashboard"))

        return render_template("setup.html", token_required=bool(expected_token))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        """利用者（user ロール）の新規登録。"""
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""

            if not username or not password:
                flash("ユーザー名とパスワードを入力してください。", "error")
            elif password != confirm:
                flash("パスワードが一致しません。", "error")
            elif User.query.filter_by(username=username).first() is not None:
                flash("そのユーザー名は既に使われています。", "error")
            else:
                user = User(username=username, role=ROLE_USER)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash("登録が完了しました。ログインしてください。", "success")
                return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """利用者用ログインページ。"""
        if current_user() is not None:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if user.is_admin:
                    # 管理者は管理者用ログインを使う
                    flash("管理者は管理者ログインページからログインしてください。", "error")
                    return redirect(url_for("admin_login"))
                session.clear()
                session["user_id"] = user.id
                flash(f"ようこそ、{user.username} さん。", "success")
                return redirect(url_for("dashboard"))

            flash("ユーザー名またはパスワードが正しくありません。", "error")

        return render_template("login.html")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        """管理者用ログインページ。"""
        user = current_user()
        if user is not None:
            return redirect(url_for("admin_users" if user.is_admin else "dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if not user.is_admin:
                    # 一般利用者はこのページからログインできない
                    flash("このページは管理者専用です。利用者ログインをご利用ください。", "error")
                    return redirect(url_for("login"))
                session.clear()
                session["user_id"] = user.id
                flash(f"管理者としてログインしました（{user.username}）。", "success")
                return redirect(url_for("admin_users"))

            flash("ユーザー名またはパスワードが正しくありません。", "error")

        return render_template("admin_login.html")

    @app.route("/logout")
    def logout():
        was_admin = (current_user() or None) and current_user().is_admin
        session.clear()
        flash("ログアウトしました。", "success")
        return redirect(url_for("admin_login" if was_admin else "login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user = current_user()
        return render_template(
            "dashboard.html",
            user=user,
            summary=personal_summary(user),
            admin=admin_summary() if user.is_admin else None,
        )

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        """利用者が自分のログインIDとパスワードを変更する。"""
        user = current_user()

        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            new_username = (request.form.get("username") or "").strip()
            new_password = request.form.get("new_password") or ""
            confirm = request.form.get("confirm") or ""

            # 本人確認のため現在のパスワードを必須にする
            if not user.check_password(current_password):
                flash("現在のパスワードが正しくありません。", "error")
            elif not new_username:
                flash("ログインIDを入力してください。", "error")
            elif (
                new_username != user.username
                and User.query.filter_by(username=new_username).first() is not None
            ):
                flash("そのログインIDは既に使われています。", "error")
            elif new_password and new_password != confirm:
                flash("新しいパスワードが一致しません。", "error")
            else:
                user.username = new_username
                if new_password:
                    user.set_password(new_password)
                db.session.commit()
                msg = "ログインIDを更新しました。"
                if new_password:
                    msg = "ログインIDとパスワードを更新しました。"
                flash(msg, "success")
                return redirect(url_for("settings"))

        return render_template("settings.html", user=user)

    # --- 管理者専用 ------------------------------------------------------
    @app.route("/admin/mypage", methods=["GET", "POST"])
    @admin_required
    def admin_mypage():
        """管理者のマイページ。アカウント情報の確認とログインID・パスワードの変更。"""
        user = current_user()

        if request.method == "POST":
            action = request.form.get("action")
            current_password = request.form.get("current_password") or ""

            # どの変更でも本人確認のため現在のパスワードを必須にする
            if not user.check_password(current_password):
                flash("現在のパスワードが正しくありません。", "error")
            elif action == "username":
                new_username = (request.form.get("username") or "").strip()
                if not new_username:
                    flash("ログインIDを入力してください。", "error")
                elif new_username == user.username:
                    flash("ログインIDが変わっていません。", "error")
                elif User.query.filter_by(username=new_username).first() is not None:
                    flash("そのログインIDは既に使われています。", "error")
                else:
                    user.username = new_username
                    db.session.commit()
                    flash(f"ログインIDを「{new_username}」に変更しました。", "success")
                    return redirect(url_for("admin_mypage"))
            elif action == "password":
                new_password = request.form.get("new_password") or ""
                confirm = request.form.get("confirm") or ""
                if len(new_password) < 8:
                    flash("新しいパスワードは 8 文字以上にしてください。", "error")
                elif new_password != confirm:
                    flash("新しいパスワードが一致しません。", "error")
                elif new_password == current_password:
                    flash("現在と同じパスワードです。別のパスワードを設定してください。", "error")
                else:
                    user.set_password(new_password)
                    db.session.commit()
                    flash("パスワードを変更しました。", "success")
                    return redirect(url_for("admin_mypage"))
            else:
                flash("不正な操作です。", "error")

        return render_template("admin_mypage.html", user=user, stats=_admin_mypage_stats(user))

    @app.route("/admin/settings")
    @admin_required
    def admin_settings():
        """旧・管理者設定ページ。マイページへ統合したためリダイレクトする。"""
        return redirect(url_for("admin_mypage"))

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        users = User.query.order_by(User.created_at.asc()).all()
        return render_template("admin_users.html", users=users, roles=ROLES)

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_create_user():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") or ROLE_USER

        if role not in ROLES:
            role = ROLE_USER

        if not username or not password:
            flash("ユーザー名とパスワードを入力してください。", "error")
        elif User.query.filter_by(username=username).first() is not None:
            flash("そのユーザー名は既に使われています。", "error")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"ユーザー「{username}」を作成しました。", "success")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_update_role(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            flash("ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        new_role = request.form.get("role")
        if new_role not in ROLES:
            flash("無効なロールです。", "error")
            return redirect(url_for("admin_users"))

        # 最後の管理者を降格させないよう保護
        if user.is_admin and new_role != ROLE_ADMIN and _admin_count() <= 1:
            flash("最後の管理者の権限は変更できません。", "error")
            return redirect(url_for("admin_users"))

        user.role = new_role
        db.session.commit()
        flash(f"「{user.username}」のロールを {new_role} に変更しました。", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            flash("ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        if user.id == current_user().id:
            flash("自分自身は削除できません。", "error")
            return redirect(url_for("admin_users"))

        if user.is_admin and _admin_count() <= 1:
            flash("最後の管理者は削除できません。", "error")
            return redirect(url_for("admin_users"))

        db.session.delete(user)
        db.session.commit()
        flash(f"ユーザー「{user.username}」を削除しました。", "success")
        return redirect(url_for("admin_users"))


def _admin_count() -> int:
    return User.query.filter_by(role=ROLE_ADMIN).count()


def _admin_mypage_stats(user: User) -> dict:
    """マイページに表示するアカウント情報のまとめ。"""
    return {
        "admin_count": _admin_count(),
        "user_count": User.query.count(),
        "report_count": DailyReport.query.filter_by(user_id=user.id).count(),
        "submitted_count": DailyReport.query.filter_by(
            user_id=user.id, status=STATUS_SUBMITTED
        ).count(),
        "comment_count": ReportComment.query.filter_by(user_id=user.id).count(),
    }


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
