import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib.util import find_spec
from io import BytesIO

import click
import requests
from flask import Blueprint, Flask, Response, abort, flash, g, redirect, render_template, request, send_file, send_from_directory, session, stream_with_context, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash
from storage_config import storage_paths
from personal_projects import project_names
from client_distribution import CLIENT_WHEEL_NAME, build_client_wheel

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR, DATA_ROOT = storage_paths()
STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
# Persist the signing key so sessions survive a manager restart.
key_path = STATE_DIR / "secret.key"
try:
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    pass
else:
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32))

app = Flask(__name__)
app.register_blueprint(Blueprint("ui", __name__, static_folder="tracknest-ui/assets", static_url_path="/static/ui"))
app.config.update(
    SECRET_KEY=key_path.read_text(),
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{STATE_DIR / 'manager.db'}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_NAME="tracknest_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("MANAGER_HTTPS") == "1",
    MAX_CONTENT_LENGTH=16 * 1024,
)
db = SQLAlchemy(app)
PORT_START = 9002
PORT_END = 9141
USERNAME_PATTERN = r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}"


class TrackioUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    nickname = db.Column(db.String(64), nullable=False, default="")
    password_hash = db.Column(db.String(256))
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    write_token = db.Column(db.String(64), nullable=False)
    port = db.Column(db.Integer, unique=True, nullable=False)
    pid = db.Column(db.Integer, default=0)
    data_dir = db.Column(db.String(1024), nullable=False)

    @property
    def display_name(self):
        return f"{self.username} ({self.nickname})" if self.nickname else self.username

    @property
    def backend_path(self):
        return f"/u/{self.username}"


class SharedProject(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=lambda: secrets.token_hex(16))
    name = db.Column(db.String(64), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("trackio_user.id"), nullable=False)
    owner = db.relationship(TrackioUser)
    write_token = db.Column(db.String(64), nullable=False)
    port = db.Column(db.Integer, unique=True, nullable=False)
    pid = db.Column(db.Integer, default=0)
    data_dir = db.Column(db.String(1024), nullable=False)
    __table_args__ = (db.UniqueConstraint("owner_id", "name"),)

    @property
    def backend_path(self):
        return f"/p/{self.id}"


class ProjectMember(db.Model):
    project_id = db.Column(db.String(32), db.ForeignKey("shared_project.id"), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("trackio_user.id"), primary_key=True)
    project = db.relationship(SharedProject)
    user = db.relationship(TrackioUser)
    role = db.Column(db.String(16), nullable=False)
    # Keep the old column for database/backup compatibility; clients use the account token.
    _legacy_token = db.Column("token", db.String(64), unique=True, nullable=False, default=lambda: secrets.token_hex(32))
    __table_args__ = (db.CheckConstraint("role IN ('owner', 'editor', 'viewer')"),)

    @property
    def token(self):
        return self.user.write_token


class UserInvitation(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("trackio_user.id"), primary_key=True)
    user = db.relationship(TrackioUser)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)


@contextmanager
def instance_lock():
    with open(STATE_DIR / "instances.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def init_db():
    db.create_all()
    # Existing accounts remain locked until an administrator assigns a password.
    columns = {c["name"] for c in inspect(db.engine).get_columns("trackio_user")}
    if "nickname" not in columns:
        db.session.execute(text("ALTER TABLE trackio_user ADD COLUMN nickname VARCHAR(64) NOT NULL DEFAULT ''"))
    if "password_hash" not in columns:
        db.session.execute(text("ALTER TABLE trackio_user ADD COLUMN password_hash VARCHAR(256)"))
    if "is_admin" not in columns:
        db.session.execute(text("ALTER TABLE trackio_user ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
    db.session.commit()


def get_free_port():
    reserved = {u.port for u in TrackioUser.query.all()}
    reserved.update(p.port for p in SharedProject.query.all())
    for port in range(PORT_START, PORT_END + 1):
        if port in reserved:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(t("端口池耗尽", "No instance ports are available"))


def is_running(user):
    if not user.pid or user.pid <= 0:
        return False
    try:
        # Check identity before signaling a persisted PID, which the OS may reuse.
        env = Path(f"/proc/{user.pid}/environ").read_bytes().split(b"\0")
        cmd = Path(f"/proc/{user.pid}/cmdline").read_bytes().split(b"\0")
        return (
            str(BASE_DIR / "trackio_server.py").encode() in cmd
            and f"TRACKIO_DIR={user.data_dir}".encode() in env
            and f"TRACKIO_WRITE_TOKEN={user.write_token}".encode() in env
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False


def start_trackio(user):
    if is_running(user):
        return
    data_dir = Path(user.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    for name in ("SPACE_ID", "SYSTEM", "PERSISTANT_STORAGE_ENABLED", "TRACKIO_SPACE_ID"):
        env.pop(name, None)
    env["TRACKIO_DIR"] = user.data_dir
    env["TRACKIO_WRITE_TOKEN"] = user.write_token
    for theme in ("LIGHT", "DARK"):
        env.setdefault(f"TRACKIO_LOGO_{theme}_URL", f"{user.backend_path}/static/trackio/trackio_logo_type_{theme.lower()}_transparent.png")
    with open(data_dir / "server.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "trackio_server.py"), str(user.port), user.backend_path],
            cwd=user.data_dir, env=env, start_new_session=True,
            stdout=log, stderr=subprocess.STDOUT,
        )
    user.pid = proc.pid
    db.session.commit()
    deadline = time.monotonic() + 30
    with requests.Session() as client:
        client.trust_env = False
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                response = client.get(f"http://127.0.0.1:{user.port}/version", timeout=1)
                if response.ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    user.pid = 0
    db.session.commit()
    raise RuntimeError(t("Trackio 启动失败，请检查该用户数据目录下的 server.log", "Trackio failed to start. Check server.log in the user data directory"))


def stop_trackio(user):
    if is_running(user):
        os.killpg(user.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while is_running(user) and time.monotonic() < deadline:
            time.sleep(0.1)
        if is_running(user):
            os.killpg(user.pid, signal.SIGKILL)
    user.pid = 0
    db.session.commit()


def validated_nickname(nickname):
    nickname = nickname.strip()
    if len(nickname) > 64:
        abort(400, t("昵称最多 64 个字符", "Nickname must not exceed 64 characters"))
    return nickname


def rename_user(user, username, nickname):
    if not re.fullmatch(USERNAME_PATTERN, username):
        abort(400, t("用户名格式无效", "Invalid username"))
    if username == user.username:
        user.nickname = nickname
        db.session.commit()
        return
    if TrackioUser.query.filter_by(username=username).first():
        abort(400, t("用户名已存在", "Username already exists"))
    projects = SharedProject.query.filter_by(owner_id=user.id).all()
    if any(is_running(backend) for backend in [user, *projects]):
        abort(400, t("请先停止该用户的个人实例及其拥有的团队项目", "Stop this user's personal instance and owned team projects first"))
    source, destination = Path(user.data_dir), DATA_ROOT / username
    if destination.exists() or destination.is_symlink():
        abort(400, t("目标数据目录已存在，未覆盖", "The target data directory already exists; nothing was overwritten"))
    project_paths = [(project, destination / Path(project.data_dir).relative_to(source)) for project in projects]
    aliases = [(link, os.readlink(link), link.resolve().relative_to(source))
               for link in (DATA_ROOT / "_shared").rglob("*")
               if link.is_symlink() and link.resolve().is_relative_to(source)]
    moved = False
    try:
        if source.exists():
            source.rename(destination)
            moved = True
        for link, _, relative in aliases:
            link.unlink()
            link.symlink_to(os.path.relpath(destination / relative, link.parent), target_is_directory=True)
        user.username, user.data_dir, user.pid = username, str(destination), 0
        user.nickname = nickname
        for project, target in project_paths:
            project.data_dir, project.pid = str(target), 0
        db.session.commit()
    except (OSError, SQLAlchemyError):
        db.session.rollback()
        if moved:
            destination.rename(source)
        for link, target, _ in aliases:
            link.unlink(missing_ok=True)
            link.symlink_to(target, target_is_directory=True)
        raise


def create_user(username, password, admin=False, nickname=""):
    nickname = validated_nickname(nickname)
    if not re.fullmatch(USERNAME_PATTERN, username):
        raise ValueError(t("用户名限 1–64 位字母、数字、下划线或连字符，首位须为字母或数字", "Use 1–64 letters, numbers, underscores or hyphens; start with a letter or number"))
    if password is not None and len(password) < 12:
        raise ValueError(t("密码至少需要 12 个字符", "Password must contain at least 12 characters"))
    if TrackioUser.query.filter_by(username=username).first():
        raise ValueError(t("用户已存在", "This username already exists"))
    user = TrackioUser(
        username=username, nickname=nickname, password_hash=generate_password_hash(password) if password is not None else None,
        is_admin=admin, write_token=secrets.token_hex(32),
        port=get_free_port(), data_dir=str(DATA_ROOT / username),
    )
    db.session.add(user)
    db.session.commit()
    return user


@app.before_request
def authenticate():
    host, _, port = request.host.rpartition(":")
    if port == "9001" and request.endpoint != "trackio_auth":
        # Browser/API access must go through Nginx so dashboard routes are served.
        path = request.path
        if request.query_string:
            path += "?" + request.query_string.decode("latin1")
        return redirect(f"{request.scheme}://{host}:9000{path}", code=307)
    g.language = "en" if request.cookies.get("tracknest_language") == "en" else "zh"
    g.user = db.session.get(TrackioUser, session.get("user_id")) if session.get("user_id") else None
    if request.endpoint in ("proxy_personal", "proxy_project"):
        request.max_content_length = 100 * 1024 * 1024
        return
    if request.endpoint in ("trackio_auth", "client_auth", "client_instance", "client_projects"):
        return
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
            abort(400, t("无效的 CSRF Token，请刷新页面", "Invalid CSRF token. Refresh the page and try again"))
    if request.endpoint not in ("login", "register", "inspect_registration", "set_language", "static", "ui.static", "trackio_asset", "client_wheel") and not g.user:
        return redirect(url_for("login"))


@app.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def t(zh, en, **values):
    return (en if g.get("language") == "en" else zh).format(**values)


@app.context_processor
def template_helpers():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return {"csrf_token": session["csrf_token"], "is_running": is_running, "t": t,
            "client_wheel_name": CLIENT_WHEEL_NAME}


@app.post("/language")
def set_language():
    language = request.form.get("language")
    if language not in ("zh", "en"):
        abort(400)
    target = request.form.get("next", "/")
    response = redirect(target if target in ("/", "/login", "/admin/users", "/register", "/profile", "/personal/projects") or re.fullmatch(r"/projects/[a-f0-9]{32}|/register#invite=[A-Za-z0-9_-]{43}", target) else url_for("index"))
    response.set_cookie(
        "tracknest_language", language, max_age=365 * 24 * 60 * 60,
        httponly=True, samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"],
    )
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = TrackioUser.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and user.password_hash and check_password_hash(user.password_hash, request.form.get("password", "")):
            session.clear()
            session["user_id"] = user.id
            return redirect(url_for("index"))
        flash(t("用户名或密码错误", "Incorrect username or password"))
        return render_template("login.html"), 401
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    memberships = ProjectMember.query.filter_by(user_id=g.user.id).all()
    return render_template("index.html", users=[g.user], memberships=memberships)


@app.get("/personal/projects")
def personal_projects():
    with instance_lock():
        db.session.refresh(g.user)
        try:
            projects = project_names(Path(g.user.data_dir))
        except (OSError, ValueError):
            app.logger.exception("Could not list personal experiment projects")
            abort(500, t("无法读取个人实验项目，请检查管理日志", "Could not list personal projects. Check the management log"))
        candidates = TrackioUser.query.filter(TrackioUser.password_hash.is_not(None),
                                              TrackioUser.id != g.user.id).order_by(TrackioUser.username).all()
        return render_template("personal_projects.html", projects=projects, running=is_running(g.user), candidates=candidates)


@app.post("/personal/projects/share")
def share_personal_project():
    with instance_lock():
        db.session.refresh(g.user)
        if is_running(g.user):
            abort(409, t("请先暂停训练并停止个人实例，再邀请协作者", "Pause training and stop the personal instance before inviting collaborators"))
        root = Path(g.user.data_dir)
        name = request.form.get("project", "")
        if name not in project_names(root):
            abort(404, t("个人实验项目不存在", "Personal experiment project not found"))
        if not re.fullmatch(USERNAME_PATTERN, name):
            abort(400, t("请先在 Dashboard 将项目改为符合团队项目规则的名称", "Rename the project in the dashboard to use a valid team project name first"))
        user = TrackioUser.query.filter_by(username=request.form.get("username", "").strip()).first()
        role = request.form.get("role")
        if not user or not user.password_hash or user.id == g.user.id or role not in ("editor", "viewer"):
            abort(400, t("请选择已有用户及编辑或只读权限", "Choose an existing user and an editor or viewer role"))
        if SharedProject.query.filter_by(owner_id=g.user.id, name=name).first():
            abort(409, t("已存在同名团队项目", "A team project with this name already exists"))
        try:
            port = get_free_port()
        except RuntimeError as exc:
            abort(503, str(exc))
        project_id = secrets.token_hex(16)
        destination = root / "projects" / project_id
        project = SharedProject(id=project_id, name=name, owner=g.user, port=port,
                                write_token=secrets.token_hex(32), data_dir=str(destination))
        command = [sys.executable, str(BASE_DIR / "personal_projects.py"), str(root), name]
        moved = False
        try:
            db.session.add(project)
            db.session.add_all([ProjectMember(project=project, user=g.user, role="owner"),
                                ProjectMember(project=project, user=user, role=role)])
            db.session.flush()
            result = subprocess.run([*command, "--share-to", str(destination)], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            moved = True
            db.session.commit()
        except (OSError, RuntimeError, SQLAlchemyError):
            db.session.rollback()
            if moved:
                try:
                    result = subprocess.run([*command, "--restore-from", str(destination)], capture_output=True, text=True)
                    if result.returncode:
                        app.logger.error("Personal project restore failed: %s", result.stderr)
                except OSError:
                    app.logger.exception("Could not restore personal project data")
            app.logger.exception("Personal project sharing failed")
            abort(500, t("转换未完成，请检查管理日志及项目目录", "Conversion did not complete. Check the management log and project directories"))
    flash(t("已邀请 {username}，项目数据已保留；原个人上报 URL 和账号 Token 继续可用。启动团队实例后协作者即可访问。",
            "Invited {username}. Data is preserved and your original personal URL and account token still work. Start the team instance for collaborators.", username=user.username))
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/personal/projects/delete")
def delete_personal_project():
    with instance_lock():
        # Authentication happened before taking the lock; reload any concurrent start.
        db.session.refresh(g.user)
        if is_running(g.user):
            abort(409, t("请先停止个人实例并暂停训练上报，再删除实验项目", "Stop your personal instance and pause training uploads before deleting a project"))
        name = request.form.get("project", "")
        try:
            projects = project_names(Path(g.user.data_dir))
        except (OSError, ValueError):
            app.logger.exception("Could not list personal experiment projects")
            abort(500, t("无法读取个人实验项目，请检查管理日志", "Could not list personal projects. Check the management log"))
        if name not in projects:
            abort(404, t("个人实验项目不存在", "Personal experiment project not found"))
        if request.form.get("confirm_name") != name:
            abort(400, t("请输入完整且一致的项目名确认删除", "Enter the exact project name to confirm deletion"))
        try:
            result = subprocess.run([sys.executable, str(BASE_DIR / "personal_projects.py"), g.user.data_dir, name],
                                    capture_output=True, text=True)
            if result.returncode:
                app.logger.error("Personal project deletion failed: %s", result.stderr)
                abort(500, t("删除未完成，部分文件可能已删除。请检查管理日志及剩余数据", "Deletion did not complete; some files may have been removed. Check the management log and remaining data"))
        except OSError:
            app.logger.exception("Could not start personal project deletion")
            abort(500, t("无法启动删除操作，请检查管理日志", "Could not start deletion. Check the management log"))
    flash(t("已永久删除个人实验项目 {name}", "Permanently deleted personal experiment project {name}", name=name))
    return redirect(url_for("personal_projects"))


def require_admin():
    if not g.user.is_admin:
        abort(403)


def admin_users_page(**context):
    projects_by_owner = {}
    for project in SharedProject.query.order_by(SharedProject.name).all():
        projects_by_owner.setdefault(project.owner_id, []).append({"id": project.id, "name": project.name})
    return render_template("admin_users.html", users=TrackioUser.query.order_by(TrackioUser.id).all(),
                           projects_by_owner=projects_by_owner, **context)


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        with instance_lock():
            g.user.nickname = validated_nickname(request.form.get("nickname", ""))
            db.session.commit()
        flash(t("昵称已更新", "Nickname updated"))
        return redirect(url_for("profile"))
    return render_template("profile.html")


@app.post("/admin/users/<int:uid>/edit")
def edit_user(uid):
    require_admin()
    nickname = validated_nickname(request.form.get("nickname", ""))
    with instance_lock():
        user = db.get_or_404(TrackioUser, uid)
        try:
            rename_user(user, request.form.get("username", "").strip(), nickname)
        except (OSError, SQLAlchemyError):
            db.session.rollback()
            app.logger.exception("Account update failed")
            abort(400, t("更新失败，原数据已保留，请检查目录权限后重试", "Update failed; original data was retained. Check directory permissions and retry"))
    flash(t("账号已更新；改名后请更新个人实验上报 URL", "Account updated. After renaming, update your personal experiment upload URL"))
    return redirect(url_for("admin_users"))


def invitation_page(user):
    token = secrets.token_urlsafe(32)
    invitation = db.session.get(UserInvitation, user.id)
    if invitation is None:
        invitation = UserInvitation(user=user)
        db.session.add(invitation)
    invitation.token_hash = hashlib.sha256(token.encode()).hexdigest()
    invitation.expires_at = int(time.time()) + 7 * 24 * 60 * 60
    db.session.commit()
    return render_template("invitation.html", user=user,
                           invitation_url=request.host_url + "register#invite=" + token)


@app.get("/admin/users")
def admin_users():
    require_admin()
    return admin_users_page()


@app.post("/admin/users/<int:uid>/invitation")
def renew_user_invitation(uid):
    require_admin()
    with instance_lock():
        user = db.get_or_404(TrackioUser, uid)
        if user.password_hash:
            abort(400, t("用户已完成注册", "This user is already registered"))
        return invitation_page(user)


def registration_invitation(token):
    invitation = UserInvitation.query.filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    if not invitation or invitation.expires_at <= int(time.time()) or invitation.user.password_hash:
        abort(400, t("邀请链接无效、已使用或已过期", "The invitation is invalid, already used or expired"))
    return invitation


@app.post("/register/inspect")
def inspect_registration():
    invitation = registration_invitation(request.form.get("token", ""))
    return {"username": invitation.user.username}


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")
    with instance_lock():
        invitation = registration_invitation(request.form.get("token", ""))
        password = request.form.get("password", "")
        if len(password) < 12 or password != request.form.get("confirm_password"):
            abort(400, t("密码至少 12 个字符，且两次输入须一致", "Use at least 12 characters and matching passwords"))
        invitation.user.password_hash = generate_password_hash(password)
        username = invitation.user.username
        db.session.delete(invitation)
        db.session.commit()
    session.clear()
    flash(t("账号 {username} 注册成功，请登录", "Account {username} is ready. Please sign in", username=username))
    return redirect(url_for("login"))


@app.get("/static/trackio/<filename>")
def trackio_asset(filename):
    # The upstream frontend uses root-relative URLs for its public loading logos.
    return send_from_directory(Path(find_spec("trackio").origin).parent / "assets", filename)


@app.get(f"/client/{CLIENT_WHEEL_NAME}")
def client_wheel():
    return send_file(BytesIO(build_client_wheel()), mimetype="application/zip",
                     as_attachment=True, download_name=CLIENT_WHEEL_NAME)


@app.post("/add")
def add_user():
    require_admin()
    try:
        with instance_lock():
            user = create_user(request.form.get("username", "").strip(), None, nickname=request.form.get("nickname", ""))
            return invitation_page(user)
    except (ValueError, RuntimeError) as exc:
        flash(str(exc))
    return redirect(url_for("admin_users"))


def managed_user(uid):
    user = db.get_or_404(TrackioUser, uid)
    if user.id != g.user.id:
        abort(403)
    return user


@app.post("/start/<int:uid>")
def start(uid):
    with instance_lock():
        user = managed_user(uid)
        try:
            start_trackio(user)
            flash(t("已启动 {username}", "Started {username}", username=user.username))
        except (OSError, RuntimeError) as exc:
            app.logger.exception("Trackio startup failed")
            flash(str(exc))
    return redirect(url_for("index"))


@app.post("/stop/<int:uid>")
def stop(uid):
    with instance_lock():
        user = managed_user(uid)
        stop_trackio(user)
        flash(t("已停止 {username}", "Stopped {username}", username=user.username))
    return redirect(url_for("index"))


@app.post("/delete/<int:uid>")
def delete(uid):
    if not g.user.is_admin:
        abort(403)
    with instance_lock():
        user = db.get_or_404(TrackioUser, uid)
        if user.is_admin:
            abort(400, t("不能从网页删除管理员", "Administrator accounts cannot be deleted from this page"))
        projects = SharedProject.query.filter_by(owner_id=user.id).order_by(SharedProject.name).all()
        if set(request.form.getlist("confirmed_project_ids")) != {project.id for project in projects}:
            flash(t("该用户拥有团队项目：{names}。请逐一确认后再删除；项目列表如有变化，请刷新重试。",
                    "This user owns team projects: {names}. Confirm each project before deleting; refresh if the list has changed.",
                    names=", ".join(project.name for project in projects) or t("无", "none")))
            return redirect(url_for("admin_users"))
        for project in projects:
            stop_trackio(project)
        stop_trackio(user)
        for project in projects:
            ProjectMember.query.filter_by(project_id=project.id).delete()
            db.session.delete(project)
        ProjectMember.query.filter_by(user_id=user.id).delete()
        UserInvitation.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
    flash(t("用户已删除，数据目录保留；同名账号重建后将继续使用原数据", "Account deleted. Data is retained and will be reused if the same username is created again"))
    return redirect(url_for("admin_users"))


@app.get("/_trackio/auth")
def trackio_auth():
    # Nginx overwrites this header; no upstream data or token is returned publicly.
    path = request.headers.get("X-Original-Path") or request.headers.get("X-Original-URI", "").split("?", 1)[0]
    backend, token = backend_access(path, request.headers.get("X-Original-Method", ""), request.headers.get("X-External-Origin"))
    return "", 204, {"X-Trackio-Port": str(backend.port), "X-Trackio-Token": token}


def backend_access(path, method, expected_origin, *, require_running=True):
    if path.startswith("/p/"):
        return shared_project_auth(path, method, expected_origin, require_running=require_running)
    match = re.fullmatch(r"/u/(" + USERNAME_PATTERN + r")(?:/.*)?", path)
    if not match:
        abort(403)
    user = TrackioUser.query.filter_by(username=match[1]).first()
    if not user:
        abort(403)
    token = request.headers.get("X-Trackio-Write-Token", "")
    if token:
        if not secrets.compare_digest(token.encode(), user.write_token.encode()):
            abort(403)
    elif not g.user or g.user.id != user.id:
        abort(403 if g.user else 401)
    else:
        # Browser writes must originate from this same site.
        origin = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and origin != expected_origin):
            abort(403)
    if require_running and not is_running(user):
        abort(503, t("实验实例未运行，请返回管理面板启动实例", "The experiment instance is not running. Start it from the management panel"))
    return user, user.write_token


# Only these read APIs are available to viewers in the pinned Trackio version.
# Unknown/new endpoints are denied until their behavior has been reviewed.
PROJECT_READ_APIS = frozenset({
    "get_run_mutation_status", "get_all_projects", "get_runs_for_project",
    "get_run_configs", "get_metrics_for_run", "get_metric_values", "get_alerts",
    "get_project_summary", "get_run_summary", "get_console_output",
    "get_system_metrics_for_run", "get_system_logs", "get_system_logs_batch",
    "get_snapshot", "get_logs", "get_logs_batch", "get_traces", "get_trace_steps",
    "query_project", "get_settings", "get_project_files", "get_tab_availability",
    "get_artifact_manifest", "get_artifacts", "list_artifacts", "get_run_artifacts",
    "get_run_artifact_counts", "get_artifact_consumers", "get_artifact_lineage",
    "get_registry_buckets", "get_registries", "get_registry_details",
})


def shared_project_auth(path, method, expected_origin, *, require_running=True):
    match = re.fullmatch(r"/p/([a-f0-9]{32})(/.*)", path)
    if not match:
        abort(403)
    project = db.session.get(SharedProject, match[1])
    if not project:
        abort(403)
    token = request.headers.get("X-Trackio-Write-Token", "")
    if token:
        user = TrackioUser.query.filter_by(write_token=token).first()
        member = db.session.get(ProjectMember, (project.id, user.id)) if user else None
    else:
        member = db.session.get(ProjectMember, (project.id, g.user.id)) if g.user else None
        origin = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and origin != expected_origin):
            abort(403)
    if not member:
        abort(403 if token or g.user else 401)
    if member.role == "viewer":
        resource = match[2]
        read_api = method == "POST" and resource.startswith("/api/") and resource[5:] in PROJECT_READ_APIS
        read_file = method in ("GET", "HEAD") and (
            resource in ("/", "/version", "/file", "/config.json", "/__trackio/frontend_version", "/__tracknest/auth")
            or resource.startswith(("/static/trackio/", "/artifact_blob/", "/assets/"))
        )
        if not read_api and not read_file:
            abort(403)
    if require_running and not is_running(project):
        abort(503, t("实验实例未运行，请返回项目管理页面启动实例", "The experiment instance is not running. Start it from the project management page"))
    return project, project.write_token if member.role != "viewer" else ""


@app.get("/__tracknest/projects")
def client_projects():
    token = request.headers.get("X-Trackio-Write-Token")
    if not token:
        abort(401)
    user = TrackioUser.query.filter_by(write_token=token).first()
    if not user:
        abort(403)
    members = ProjectMember.query.filter_by(user_id=user.id).join(SharedProject).order_by(SharedProject.name, SharedProject.id).all()
    return {"projects": [{"id": member.project_id, "name": member.project.name, "owner": member.project.owner.username}
                         for member in members]}


@app.get("/u/<username>/__tracknest/auth")
@app.get("/p/<project_id>/__tracknest/auth")
def client_auth(username=None, project_id=None):
    if not request.headers.get("X-Trackio-Write-Token"):
        abort(401)
    backend, _ = backend_access(request.path, "GET", request.host_url.rstrip("/"), require_running=False)
    return {"authenticated": True, "instance_running": is_running(backend)}


@app.post("/u/<username>/__tracknest/instance")
@app.post("/p/<project_id>/__tracknest/instance")
def client_instance(username=None, project_id=None):
    if not request.headers.get("X-Trackio-Write-Token"):
        abort(401)
    payload = request.get_json(silent=True)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in ("start", "stop"):
        abort(400)
    with instance_lock():
        db.session.expire_all()
        backend, _ = backend_access(request.path, "POST", request.host_url.rstrip("/"), require_running=False)
        if project_id and action == "stop":
            user = TrackioUser.query.filter_by(write_token=request.headers["X-Trackio-Write-Token"]).first()
            if user.id != backend.owner_id:
                abort(403)
        try:
            (start_trackio if action == "start" else stop_trackio)(backend)
        except (OSError, RuntimeError) as exc:
            app.logger.exception("Client instance %s failed", action)
            abort(503, str(exc))
        return {"instance_running": is_running(backend)}



PROXY_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@app.route("/u/<username>/", defaults={"tail": ""}, methods=PROXY_METHODS)
@app.route("/u/<username>/<path:tail>", methods=PROXY_METHODS)
def proxy_personal(username, tail):
    return proxy_backend(tail)


@app.route("/p/<project_id>/", defaults={"tail": ""}, methods=PROXY_METHODS)
@app.route("/p/<project_id>/<path:tail>", methods=PROXY_METHODS)
def proxy_project(project_id, tail):
    return proxy_backend(tail)


def proxy_backend(tail):
    backend, token = backend_access(request.path, request.method, request.host_url.rstrip("/"))
    target = f"http://127.0.0.1:{backend.port}/{tail}"
    if request.query_string:
        target += "?" + request.query_string.decode("latin1")
    excluded = {"host", "cookie", "connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-authorization", "proxy-authenticate", "te", "trailer", "content-length", "x-trackio-write-token"}
    headers = {key: value for key, value in request.headers if key.lower() not in excluded}
    headers["Host"] = request.host
    headers["X-Trackio-Write-Token"] = token
    headers["Accept-Encoding"] = "identity"
    client = requests.Session()
    client.trust_env = False
    try:
        upstream = client.request(request.method, target, headers=headers,
                                  data=request.get_data(), stream=True, allow_redirects=False, timeout=(5, 300))
    except requests.RequestException:
        client.close()
        abort(502, t("实验实例暂时无法连接，请稍后重试", "The experiment instance is temporarily unavailable"))

    def content():
        try:
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()
            client.close()

    response_headers = {key: value for key, value in upstream.headers.items()
                        if key.lower() not in excluded | {"content-encoding", "content-length", "set-cookie"}}
    response = Response(stream_with_context(content()), status=upstream.status_code, headers=response_headers)
    response.call_on_close(upstream.close)
    response.call_on_close(client.close)
    return response


def project_membership(project_id, owner_only=False):
    member = db.session.get(ProjectMember, (project_id, g.user.id))
    if not member or (owner_only and member.project.owner_id != g.user.id):
        abort(403)
    return member


@app.post("/projects")
def create_project():
    name = request.form.get("name", "").strip()
    if not re.fullmatch(USERNAME_PATTERN, name):
        abort(400, t("项目名限 1–64 位字母、数字、下划线或连字符，首位须为字母或数字", "Use 1–64 letters, numbers, underscores or hyphens; start with a letter or number"))
    with instance_lock():
        if SharedProject.query.filter_by(owner_id=g.user.id, name=name).first():
            abort(400, t("你已创建同名项目", "You already own a project with this name"))
        try:
            port = get_free_port()
        except RuntimeError as exc:
            abort(503, str(exc))
        project_id = secrets.token_hex(16)
        project = SharedProject(
            id=project_id, name=name, owner_id=g.user.id, write_token=secrets.token_hex(32),
            port=port, data_dir=str(Path(g.user.data_dir) / "projects" / project_id),
        )
        db.session.add(project)
        db.session.add(ProjectMember(project=project, user_id=g.user.id, role="owner"))
        db.session.commit()
    return redirect(url_for("project_detail", project_id=project.id))


@app.get("/projects/<project_id>")
def project_detail(project_id):
    member = project_membership(project_id)
    members = ProjectMember.query.filter_by(project_id=project_id).all()
    member_ids = [m.user_id for m in members]
    candidates = TrackioUser.query.filter(TrackioUser.password_hash.is_not(None), TrackioUser.id.not_in(member_ids)).order_by(TrackioUser.username).all() if member.role == "owner" else []
    return render_template("project.html", project=member.project, membership=member, members=members, candidates=candidates)


@app.post("/projects/<project_id>/members")
def set_project_member(project_id):
    with instance_lock():
        project = project_membership(project_id, owner_only=True).project
        user = TrackioUser.query.filter_by(username=request.form.get("username", "").strip()).first()
        role = request.form.get("role")
        if not user or not user.password_hash or user.id == project.owner_id or role not in ("editor", "viewer"):
            abort(400, t("请填写已有用户，并选择编辑或只读权限；所有者权限不可修改", "Choose an existing user and an editor or viewer role. The owner's role cannot be changed"))
        member = db.session.get(ProjectMember, (project_id, user.id))
        if member:
            member.role = role
        else:
            db.session.add(ProjectMember(project_id=project_id, user_id=user.id, role=role))
        db.session.commit()
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/projects/<project_id>/members/<int:user_id>/remove")
def remove_project_member(project_id, user_id):
    with instance_lock():
        project = project_membership(project_id, owner_only=True).project
        if user_id == project.owner_id:
            abort(400)
        member = db.get_or_404(ProjectMember, (project_id, user_id))
        db.session.delete(member)
        db.session.commit()
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/projects/<project_id>/token")
def rotate_project_token(project_id):
    with instance_lock():
        project_membership(project_id)
        running = is_running(g.user)
        stop_trackio(g.user)
        g.user.write_token = secrets.token_hex(32)
        db.session.commit()
        if running:
            try:
                start_trackio(g.user)
            except (OSError, RuntimeError) as exc:
                app.logger.exception("Personal instance restart after token rotation failed")
                flash(str(exc))
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/projects/<project_id>/leave")
def leave_project(project_id):
    with instance_lock():
        member = project_membership(project_id)
        if member.project.owner_id == g.user.id:
            abort(400, t("所有者不能直接退出，请先删除项目", "The owner cannot leave the project. Delete it first"))
        name = member.project.name
        db.session.delete(member)
        db.session.commit()
    flash(t("已退出团队项目 {name}，项目 Token 已失效", "You left {name}. Your project token has been revoked", name=name))
    return redirect(url_for("index"))


@app.post("/projects/<project_id>/instance")
def manage_project_instance(project_id):
    with instance_lock():
        project = project_membership(project_id, owner_only=True).project
        action = request.form.get("action")
        if action not in ("start", "stop"):
            abort(400)
        try:
            (start_trackio if action == "start" else stop_trackio)(project)
        except (OSError, RuntimeError) as exc:
            app.logger.exception("Team project startup failed")
            flash(str(exc))
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/projects/<project_id>/delete")
def delete_project(project_id):
    with instance_lock():
        project = project_membership(project_id, owner_only=True).project
        stop_trackio(project)
        ProjectMember.query.filter_by(project_id=project_id).delete()
        db.session.delete(project)
        db.session.commit()
    flash(t("团队项目已删除，实验数据目录保留", "Team project deleted. Experiment data is retained"))
    return redirect(url_for("index"))


@app.cli.command("init-db")
def init_db_command():
    init_db()
    click.echo("数据库已初始化")


@app.cli.command("create-admin")
@click.argument("username")
@click.password_option(confirmation_prompt=True)
def create_admin(username, password):
    init_db()
    try:
        with instance_lock():
            create_user(username, password, admin=True)
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"管理员 {username} 已创建")


@app.cli.command("migrate-ports")
def migrate_ports():
    """Move existing instances into the current port pool, preserving data."""
    init_db()
    try:
        with instance_lock():
            for user in TrackioUser.query.order_by(TrackioUser.id).all():
                if PORT_START <= user.port <= PORT_END:
                    continue
                port = get_free_port()
                running = is_running(user)
                old_port = user.port
                stop_trackio(user)
                user.port = port
                db.session.commit()
                if running:
                    start_trackio(user)
                click.echo(f"{user.username}: {old_port} -> {port}")
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@app.cli.command("migrate-project-data")
def migrate_project_data():
    """Move project data beneath its creator's directory."""
    init_db()
    try:
        with instance_lock():
            for project in SharedProject.query.order_by(SharedProject.id).all():
                source = Path(project.data_dir)
                destination = Path(project.owner.data_dir) / "projects" / project.id
                if source == destination:
                    continue
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"目标目录已存在，未覆盖：{destination}")
                if source.exists() and not source.is_dir():
                    raise RuntimeError(f"项目数据路径不是目录：{source}")
                running = is_running(project)
                stop_trackio(project)
                moved = False
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if source.exists():
                        source.rename(destination)
                        moved = True
                        # Trackio may have stored absolute paths for media/files.
                        source.symlink_to(destination, target_is_directory=True)
                    project.data_dir = str(destination)
                    db.session.commit()
                except (OSError, SQLAlchemyError):
                    db.session.rollback()
                    if moved:
                        if source.is_symlink():
                            source.unlink()
                        destination.rename(source)
                    if running:
                        start_trackio(project)
                    raise
                if running:
                    start_trackio(project)
                click.echo(f"{project.name}: {source} -> {destination}")
    except (OSError, RuntimeError, SQLAlchemyError) as exc:
        raise click.ClickException(str(exc)) from exc


@app.cli.command("set-password")
@click.argument("username")
@click.password_option(confirmation_prompt=True)
def set_password(username, password):
    init_db()
    user = TrackioUser.query.filter_by(username=username).first()
    if not user or len(password) < 12:
        raise click.ClickException("用户不存在或密码不足 12 个字符")
    user.password_hash = generate_password_hash(password)
    db.session.commit()
    click.echo("密码已更新")


@app.cli.command("relocate-data")
def relocate_data_command():
    """Adapt copied data to the current deployment's storage directory."""
    from data_transfer import relocate_data
    try:
        relocate_data()
    except (OSError, ValueError, RuntimeError, SQLAlchemyError) as exc:
        db.session.rollback()
        raise click.ClickException("数据目录适配失败，请检查数据副本和目录权限。" if isinstance(exc, SQLAlchemyError) else str(exc)) from exc
    click.echo(f"数据目录已适配：{DATA_ROOT}；失效的实例 PID 已清理。")


@app.cli.command("export-user")
@click.argument("username")
@click.argument("destination", type=click.Path(path_type=Path))
def export_user_command(username, destination):
    """Export a stopped user's account and owned team projects."""
    from data_transfer import export_user
    try:
        export_user(username, destination)
    except (OSError, ValueError, RuntimeError, SQLAlchemyError) as exc:
        raise click.ClickException("导出失败，请检查管理数据库。" if isinstance(exc, SQLAlchemyError) else str(exc)) from exc
    click.echo(f"用户 {username} 已导出到 {destination.resolve()}，备份包含登录凭据，请妥善保管。")


@app.cli.command("import-user")
@click.argument("source", type=click.Path(exists=True, file_okay=False, path_type=Path))
def import_user_command(source):
    """Import an account as a regular user without granting other memberships."""
    from data_transfer import import_user
    try:
        user = import_user(source)
    except (OSError, ValueError, RuntimeError, SQLAlchemyError) as exc:
        raise click.ClickException("导入失败，数据库未完成导入，请检查备份。" if isinstance(exc, SQLAlchemyError) else str(exc)) from exc
    click.echo(f"用户 {user.username} 已导入：{user.data_dir}。请在团队项目中重新邀请其他成员。")


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="127.0.0.1", port=9001, debug=False)
