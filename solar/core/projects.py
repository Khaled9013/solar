"""References to host project folders. Never copy, erase, or replace a project root."""
from contextlib import contextmanager
import os
from pathlib import Path

from filelock import FileLock, Timeout

from solar.config import CONFIG_PATH, config
from solar.crypto import digest
from solar.core.vault import is_link_or_junction, safe_path, workspace_path


def require_host(user):
    if not user.is_active or not user.host_access:
        raise PermissionError("Host execution permission is required to access PC folders")


def private_path(path):
    path = Path(path).resolve()
    return path == CONFIG_PATH.resolve() or path.is_relative_to(config()["data_dir"])


def validated_root(value, create=False):
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 4096:
        raise ValueError("Enter an absolute folder path on the remote PC")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("Use an absolute local disk path; network shares are not supported")
    if is_link_or_junction(path):
        raise ValueError("Choose the actual folder, not a symbolic link or junction")
    path = path.resolve()
    if private_path(path) or path == Path(path.anchor):
        raise ValueError("Choose a project folder outside Solar's private storage, not a drive root")
    if create and not path.exists():
        safe_path(path.name)
        if not path.parent.is_dir():
            raise ValueError("Choose an existing parent folder before creating a project")
        try:
            path.mkdir()
        except OSError as exc:
            raise ValueError("This folder could not be created. Check the name and the PC account's write access to its parent.") from exc
    if not path.is_dir():
        raise ValueError("Project folder does not exist on this PC")
    return path


def project_root(project, user=None):
    if user:
        require_host(user)
        if project.owner_id != user.id:
            raise PermissionError("This project belongs to another account")
    path = validated_root(project.root_path)
    if os.path.normcase(str(path)) != os.path.normcase(project.root_path):
        raise ValueError("The project folder changed location; register its current path again")
    return path


def execution_directory(chat):
    if chat.project_id:
        return project_root(chat.project, chat.owner)
    return workspace_path(chat)


def project_file(project, value="", user=None):
    root = project_root(project, user)
    path = root
    if value:
        for part in safe_path(value).split("/"):
            path = path / part
            if is_link_or_junction(path):
                raise ValueError("Project links and junctions cannot be browsed through Solar")
    if not path.resolve().is_relative_to(root) or private_path(path):
        raise PermissionError("This path is outside the project or contains Solar private data")
    return path


def tree(project, value="", user=None):
    directory = project_file(project, value, user)
    if not directory.is_dir():
        raise ValueError("Choose a project directory")
    entries = []
    with os.scandir(directory) as items:
        for item in items:
            if len(entries) >= 500:
                break
            path = Path(item.path)
            if is_link_or_junction(path) or private_path(path):
                continue
            try:
                is_dir = item.is_dir(follow_symlinks=False)
                entries.append({"name": item.name, "path": path.relative_to(project_root(project)).as_posix(),
                                "directory": is_dir, "size": 0 if is_dir else item.stat().st_size})
            except OSError:
                continue
    return sorted(entries, key=lambda entry: (not entry["directory"], entry["name"].casefold()))


def project_lock(project):
    key = digest(os.path.normcase(str(project_root(project))), "project-lock")
    return FileLock(str(config()["data_dir"] / "locks" / f"project-{key}.lock"))


@contextmanager
def execution_guard(chat, check):
    if not chat.project_id:
        yield
        return
    lock = project_lock(chat.project)
    while True:
        check()
        try:
            lock.acquire(timeout=.25)
            break
        except Timeout:
            continue
    try:
        yield
    finally:
        lock.release()
