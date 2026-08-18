"""セッション認証のヘルパー。app.py と reports.py の両方から使う。"""
from functools import wraps

from flask import flash, redirect, session, url_for

from models import User, db


def current_user():
    """ログイン中のユーザー。未ログインなら None。"""
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db.session.get(User, user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("ログインが必要です。", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            flash("管理者ログインが必要です。", "error")
            return redirect(url_for("admin_login"))
        if not user.is_admin:
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped
