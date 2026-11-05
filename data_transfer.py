"""Offline relocation and single-user transfers for local deployments."""
import json
import os
from pathlib import Path
import re
import shutil

from app import (
    DATA_ROOT, STATE_DIR, USERNAME_PATTERN, ProjectMember, SharedProject, TrackioUser,
    db, get_free_port, instance_lock, is_running,
)


def relocate_data():
    with instance_lock():
        users = TrackioUser.query.all()
        projects = SharedProject.query.all()
        roots = {user.id: Path(user.data_dir).parent for user in users}
        changes = [(user, DATA_ROOT / user.username) for user in users]
        for project in projects:
            try:
                relative = Path(project.data_dir).relative_to(roots[project.owner_id])
            except ValueError as exc:
                raise ValueError(f"项目目录不在创建者的数据根目录中：{project.name}") from exc
            changes.append((project, DATA_ROOT / relative))
        moved = any(Path(backend.data_dir) != target for backend, target in changes)
        if moved:
            if any(is_running(backend) for backend, _ in changes):
                raise ValueError("请先停止全部实验实例，再迁移数据目录。")
            if not DATA_ROOT.is_dir():
                raise ValueError(f"请先将实验数据目录拷贝到 {DATA_ROOT}")
        # Preserve old compatibility aliases without depending on the old host path.
        for link in DATA_ROOT.rglob("*") if moved else ():
            if not link.is_symlink():
                continue
            target = Path(os.readlink(link))
            if not target.is_absolute():
                continue
            for old_root in set(roots.values()):
                if target.is_relative_to(old_root):
                    relative = os.path.relpath(DATA_ROOT / target.relative_to(old_root), link.parent)
                    link.unlink()
                    link.symlink_to(relative)
                    break
        for backend, target in changes:
            backend.data_dir = str(target)
            if not is_running(backend):
                backend.pid = 0
        db.session.commit()
        return moved


def _copy_data(source, destination):
    """Copy internal symlinks as relative links; never include external data."""
    links = []
    source = source.resolve()
    if not source.exists():
        destination.mkdir(parents=True, mode=0o700)
        return
    for link in source.rglob("*"):
        if link.is_symlink():
            target = link.resolve()
            if not target.is_relative_to(source):
                raise ValueError(f"数据包含指向目录外的符号链接，请先处理：{link}")
            links.append((link.relative_to(source), target.relative_to(source)))
    shutil.copytree(source, destination, symlinks=True)
    for relative, target in links:
        link = destination / relative
        link.unlink()
        link.symlink_to(os.path.relpath(destination / target, link.parent))


def export_user(username, destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError("导出目标已存在，请使用新的目录，避免覆盖备份。")
    destination = destination.resolve()
    if destination.is_relative_to(DATA_ROOT) or destination.is_relative_to(STATE_DIR):
        raise ValueError("请将备份导出到管理状态目录和实验数据目录之外。")
    with instance_lock():
        user = TrackioUser.query.filter_by(username=username).first()
        if not user or not user.password_hash:
            raise ValueError("只能导出已注册的用户。")
        if Path(user.data_dir) != DATA_ROOT / user.username:
            raise ValueError("数据路径仍属于旧部署，请先运行 relocate-data 适配后再导出。")
        projects = SharedProject.query.filter_by(owner_id=user.id).order_by(SharedProject.id).all()
        if any(is_running(backend) for backend in [user, *projects]):
            raise ValueError("请先停止该用户的个人实例及其拥有的团队项目。")
        manifest = {
            "version": 1,
            "user": {"username": user.username, "nickname": user.nickname, "password_hash": user.password_hash, "write_token": user.write_token},
            "projects": [],
        }
        for project in projects:
            owner = db.session.get(ProjectMember, (project.id, user.id))
            manifest["projects"].append({
                "id": project.id, "name": project.name, "write_token": project.write_token,
                "owner_token": owner.token,
            })
        destination.mkdir(parents=True, mode=0o700)
        try:
            _copy_data(Path(user.data_dir), destination / "data")
            for project in projects:
                if Path(project.data_dir).resolve() != (Path(user.data_dir) / "projects" / project.id).resolve():
                    _copy_data(Path(project.data_dir), destination / "data/projects" / project.id)
            with (destination / "user.json").open("x") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
            (destination / "user.json").chmod(0o600)
        except Exception:
            shutil.rmtree(destination)
            raise


def import_user(source):
    source = source.resolve()
    if (source / "user.json").is_symlink() or (source / "data").is_symlink():
        raise ValueError("备份清单和数据根目录不能是符号链接。")
    manifest = json.loads((source / "user.json").read_text())
    try:
        account = manifest["user"]
        projects = manifest["projects"]
        if not isinstance(account, dict) or not isinstance(projects, list):
            raise ValueError
        if manifest["version"] != 1 or not re.fullmatch(USERNAME_PATTERN, account["username"]):
            raise ValueError
        if not all(isinstance(account[key], str) and account[key] for key in ("password_hash", "write_token")):
            raise ValueError
        if not isinstance(account.get("nickname", ""), str) or len(account.get("nickname", "")) > 64:
            raise ValueError
        for project in projects:
            if not re.fullmatch(r"[a-f0-9]{32}", project["id"]) or not re.fullmatch(USERNAME_PATTERN, project["name"]):
                raise ValueError
            if not all(isinstance(project[key], str) and project[key] for key in ("write_token", "owner_token")):
                raise ValueError
        for key in ("id", "name", "owner_token"):
            if len({project[key] for project in projects}) != len(projects):
                raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("用户备份清单格式无效。") from exc
    if not (source / "data").is_dir():
        raise ValueError("备份缺少 data 目录。")
    destination = DATA_ROOT / account["username"]
    with instance_lock():
        if TrackioUser.query.filter_by(username=account["username"]).first():
            raise ValueError("同名账号已存在，未覆盖。")
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"用户数据目录已存在，未覆盖：{destination}")
        for project in projects:
            if db.session.get(SharedProject, project["id"]) or ProjectMember.query.filter_by(token=project["owner_token"]).first():
                raise ValueError("团队项目 ID 或成员 Token 已存在，未覆盖。")
        DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            _copy_data(source / "data", destination)
            user = TrackioUser(
                username=account["username"], nickname=account.get("nickname", ""), password_hash=account["password_hash"],
                write_token=account["write_token"], is_admin=False, pid=0,
                port=get_free_port(), data_dir=str(destination),
            )
            db.session.add(user)
            db.session.flush()
            for item in projects:
                project = SharedProject(
                    id=item["id"], name=item["name"], owner=user, pid=0,
                    write_token=item["write_token"], port=get_free_port(),
                    data_dir=str(destination / "projects" / item["id"]),
                )
                db.session.add(project)
                db.session.add(ProjectMember(project=project, user=user, role="owner", token=item["owner_token"]))
                db.session.flush()
            db.session.commit()
        except Exception:
            db.session.rollback()
            if destination.exists():
                shutil.rmtree(destination)
            raise
    return user
