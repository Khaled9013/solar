import os
from pathlib import Path

from filelock import Timeout

from solar.config import config
from solar.crypto import digest
from solar.core.models import Chat, Project, Schedule
from solar.core import projects
from solar.core.views import api, attachment, body, boolean, own, response, text


def serialize(project):
    return {"id": str(project.id), "name": project.name, "path": project.root_path,
            "created_at": project.created_at}


@api(("GET", "POST"))
def collection(request):
    if request.method == "GET":
        rows = Project.objects.filter(owner=request.user) if request.user.host_access else []
        return response({"projects": [serialize(p) for p in rows]})
    projects.require_host(request.user)
    data = body(request)
    name = text(data, "name", required=True, limit=160)
    path = projects.validated_root(text(data, "path", required=True, limit=4096), boolean(data, "create"))
    project = Project.objects.create(owner=request.user, name=name, root_path=str(path),
                                     path_hash=digest(os.path.normcase(str(path)), "project-path"))
    return response(serialize(project), 201)


@api(("PATCH", "DELETE"))
def detail(request, project_id):
    projects.require_host(request.user)
    project = own(Project, request.user, project_id)
    if request.method == "DELETE":
        if Chat.objects.filter(project=project).exists() or Schedule.objects.filter(project=project).exists():
            raise ValueError("Unassign this project from its sessions and automations before removing it")
        project.delete()  # unregister only; never delete the PC directory
        return response()
    data = body(request)
    project.name = text(data, "name", project.name, required=True, limit=160)
    if "path" in data and data["path"] != project.root_path:
        raise ValueError("Register a new project to use another folder; existing sessions keep their folder")
    project.save(update_fields=["name"])
    return response(serialize(project))


@api()
def directories(request):
    projects.require_host(request.user)
    value = request.GET.get("path", "")
    if not value:
        roots = [str(Path.home())]
        if os.name == "nt":
            import ctypes
            drives = ctypes.windll.kernel32.GetLogicalDrives()
            roots += [f"{chr(65 + i)}:\\" for i in range(26) if drives & (1 << i)]
        else:
            roots.append("/")
        return response({"roots": list(dict.fromkeys(roots)), "path": "", "parent": None, "directories": []})
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("Use an absolute local folder path")
    if projects.private_path(path) or projects.is_link_or_junction(path):
        raise PermissionError("Solar private storage and links cannot be selected")
    path = path.resolve()
    if not path.is_dir():
        raise ValueError("Directory does not exist")
    entries = []
    with os.scandir(path) as items:
        for item in items:
            if len(entries) >= 500:
                break
            child = Path(item.path)
            if item.is_dir(follow_symlinks=False) and not projects.is_link_or_junction(child) and not projects.private_path(child):
                entries.append({"name": item.name, "path": str(child)})
    return response({"path": str(path), "parent": str(path.parent) if path.parent != path else None,
                     "directories": sorted(entries, key=lambda item: item["name"].casefold())})


@api()
def tree(request, project_id):
    projects.require_host(request.user)
    project = own(Project, request.user, project_id)
    return response({"project": serialize(project), "entries": projects.tree(project, request.GET.get("path", ""), request.user)})


@api(("GET", "POST"))
def file(request, project_id):
    projects.require_host(request.user)
    project = own(Project, request.user, project_id)
    maximum = config()["storage"]["max_file_mb"] * 1024**2
    if request.method == "GET":
        path = projects.project_file(project, request.GET.get("path", ""), request.user)
        if not path.is_file() or path.stat().st_size > maximum:
            raise ValueError("File is missing or exceeds the download limit")
        return attachment(path.read_bytes(), path.name)
    uploads = request.FILES.getlist("files")
    paths = request.POST.getlist("paths")
    if not 1 <= len(uploads) <= 200 or len(paths) != len(uploads):
        raise ValueError("Provide 1 to 200 files with relative paths")
    targets = []
    seen = set()
    for upload, relative in zip(uploads, paths, strict=True):
        target = projects.project_file(project, relative, request.user)
        key = os.path.normcase(str(target))
        if upload.size > maximum or target.exists() or key in seen:
            raise ValueError("A file exceeds the upload limit or already exists; project uploads never overwrite files")
        seen.add(key)
        targets.append((relative, upload))
    try:
        with projects.project_lock(project).acquire(timeout=0):
            checked = [(projects.project_file(project, relative, request.user), upload) for relative, upload in targets]
            if any(path.exists() for path, _ in checked):
                raise ValueError("A project file already exists; uploads never overwrite files")
            for path, upload in checked:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    for chunk in upload.chunks():
                        output.write(chunk)
    except Timeout:
        raise ValueError("Stop the active project run before uploading project files") from None
    except FileExistsError:
        raise ValueError("A project file was created concurrently; existing files were preserved") from None
    return response({"uploaded": len(targets)})
