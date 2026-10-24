"""Encrypted file vault with a virtual tree and bounded, traversal-safe ZIP handling."""

import csv
import io
import mimetypes
import os
from pathlib import Path
import shutil
import stat
import unicodedata
import uuid
import zipfile

from django.db import transaction
from django.db.models import Sum
from filelock import FileLock

from solar.config import config
from solar.crypto import cipher, digest
from solar.core.models import StoredFile

FOLDERS = (
    "user/files", "user/images", "user/zip_files",
    "codex/files", "codex/images", "codex/zip_files", "codex/agents",
)
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in "0123456789¹²³"), *(f"LPT{i}" for i in "0123456789¹²³")}


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("A file path must contain between 1 and 240 characters")
    value = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 or c in ':<>"|?*' for c in value):
        raise ValueError("Invalid file path")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.endswith((".", " ")) or len(p) > 120
           or p.split(".")[0].rstrip(" ").upper() in RESERVED for p in parts):
        raise ValueError("Unsafe file path")
    return "/".join(parts)


def chat_directory(chat):
    return config()["data_dir"] / "chats" / str(chat.owner_id) / f"chat_{chat.created_at:%Y-%m-%d}_{chat.id}"


def ensure_tree(chat):
    root = chat_directory(chat)
    for directory in FOLDERS:
        (root / directory).mkdir(parents=True, exist_ok=True)
    return root


def vault_lock(chat):
    return FileLock(str(config()["data_dir"] / "locks" / f"vault-{chat.owner_id}.lock"), timeout=30)


def blob_path(file, legacy=True):
    # The physical name is unrelated to the user-controlled logical file name.
    folder = "/".join(file.path.split("/")[:2])
    if folder not in FOLDERS:
        folder = "codex/files"
    directory = chat_directory(file.chat) / folder
    current = directory / f"{file.id}-{file.revision}.enc"
    old = directory / f"{file.id}.enc"
    return old if legacy and not current.exists() and old.exists() else current


def file_cipher(chat):
    return cipher(f"chat-files:{chat.owner_id}:{chat.id}")


def put_file(chat, path, data):
    path = safe_path(path)
    if "/".join(path.split("/")[:2]) not in FOLDERS:
        raise ValueError("Files must be stored inside a user/ or codex/ category")
    maximum = config()["storage"]["max_file_mb"] * 1024**2
    if len(data) > maximum:
        raise ValueError(f"File exceeds the {maximum // 1024**2} MB limit")
    path_hash = digest(path.casefold(), f"path:{chat.id}")
    with vault_lock(chat), transaction.atomic():
        existing = StoredFile.objects.filter(chat=chat, path_hash=path_hash).first()
        folded = path.casefold()
        for other in StoredFile.objects.filter(chat=chat).exclude(path_hash=path_hash).only("path"):
            other_path = other.path.casefold()
            if folded.startswith(other_path + "/") or other_path.startswith(folded + "/"):
                raise ValueError("A file and a folder cannot use the same path")
        total = StoredFile.objects.filter(chat__owner_id=chat.owner_id).aggregate(n=Sum("size"))["n"] or 0
        if total - (existing.size if existing else 0) + len(data) > config()["storage"]["quota_mb"] * 1024**2:
            raise ValueError("Your file storage quota has been reached")
        record = existing or StoredFile(chat=chat, path_hash=path_hash)
        previous = blob_path(existing) if existing else None
        record.revision = uuid.uuid4()
        record.path, record.size = path, len(data)
        record.mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        ensure_tree(chat)
        destination = blob_path(record, legacy=False)
        temp = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temp.open("xb") as handle:
            handle.write(file_cipher(chat).encrypt(data))
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(destination)
        try:
            record.save()
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if previous:
            # Never replace the last committed revision before the database commits.
            transaction.on_commit(lambda: previous.unlink(missing_ok=True))
    return record


def read_file(record):
    with vault_lock(record.chat):
        # A download may have obtained this row just before a concurrent replacement.
        record.refresh_from_db(fields=["revision", "path", "size", "mime"])
        data = file_cipher(record.chat).decrypt(blob_path(record).read_bytes())
        if len(data) != record.size:
            raise ValueError("File integrity check failed")
        return data


def delete_file(record):
    with vault_lock(record.chat), transaction.atomic():
        target = blob_path(record)
        record.delete()
        transaction.on_commit(lambda: target.unlink(missing_ok=True))


def zip_members(data, max_total=None, max_files=200):
    """Validate every entry before returning any bytes. No extractall, links, or special files."""
    max_total = max_total or config()["storage"]["export_mb"] * 1024**2
    output, seen, total = [], set(), 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > max_files:
            raise ValueError(f"Archive contains more than {max_files} entries")
        for info in entries:
            name = safe_path(info.filename.rstrip("/"))
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("Archive links and special files are not allowed")
            if info.flag_bits & 1:
                raise ValueError("Password-protected ZIPs cannot be extracted")
            if name.casefold() in seen:
                raise ValueError("Archive contains duplicate paths")
            seen.add(name.casefold())
            if info.is_dir():
                continue
            total += info.file_size
            if (info.file_size > config()["storage"]["max_file_mb"] * 1024**2 or total > max_total
                    or info.file_size > max(info.compress_size, 1) * 300):
                raise ValueError("Archive exceeds extraction limits")
            content = archive.read(info)
            if len(content) != info.file_size:
                raise ValueError("Archive size mismatch")
            output.append((name, content))
    return output


def history_text(chat):
    parts = []
    for event in chat.events.order_by("id").iterator():
        parts.append(f"[{event.created_at.isoformat()}] {event.role.upper()} ({event.kind})\n{event.content}\n")
    return "\n".join(parts)


def references_csv(chat):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["file_id", "path", "bytes", "updated_at"])
    for file in chat.files.all():
        # Prevent spreadsheet formula interpretation when a CSV is opened in Excel.
        path = "'" + file.path if file.path[:1] in "=+-@\t\r" else file.path
        writer.writerow([file.id, path, file.size, file.updated_at.isoformat()])
    return stream.getvalue()


def sync_manifests(chat):
    root = ensure_tree(chat)
    for name, text in (("chat_history.txt", history_text(chat)), ("references.csv", references_csv(chat))):
        temp = root / f".{name}.{uuid.uuid4().hex}.tmp"
        temp.write_bytes(file_cipher(chat).encrypt(text.encode("utf-8")))
        temp.replace(root / f"{name}.enc")


def export_zip(chat, prefix=""):
    if prefix:
        prefix = safe_path(prefix).rstrip("/") + "/"
    files = [file for file in chat.files.all() if file.path.startswith(prefix)]
    limit = config()["storage"]["export_mb"] * 1024**2
    if sum(file.size for file in files) > limit:
        raise ValueError("Export exceeds the configured ZIP size limit; download a smaller folder")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if not prefix:
            for folder in FOLDERS:
                archive.writestr(folder + "/", b"")
            archive.writestr("chat_history.txt", history_text(chat))
            archive.writestr("references.csv", references_csv(chat))
        for file in files:
            archive.writestr(file.path, read_file(file))
    return buffer.getvalue()


def workspace_path(chat):
    return config()["data_dir"] / "runtime" / str(chat.id)


def materialize(chat):
    root = workspace_path(chat)
    if root.exists():
        raise ValueError("An unfinished workspace exists; restart Solar to recover it")
    root.mkdir(parents=True, mode=0o700)
    for folder in FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    for file in chat.files.all():
        target = root / safe_path(file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_file(file))
    (root / "chat_history.txt").write_text(history_text(chat), encoding="utf-8")
    (root / "references.csv").write_text(references_csv(chat), encoding="utf-8")
    return root


def is_link_or_junction(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def collect_workspace(chat):
    """Import regular output only. Retain the workspace on import failure for recovery."""
    root = workspace_path(chat)
    if not root.exists():
        return
    if is_link_or_junction(root):
        raise ValueError("Workspace root was replaced by a link")
    limit = config()["storage"]["max_file_mb"] * 1024**2
    ignored = {".git", ".solar-internal", ".codex-home", ".claude-home", "node_modules", ".venv", "__pycache__"}
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ignored and not is_link_or_junction(Path(directory) / name)]
        for name in files:
            path = Path(directory) / name
            if is_link_or_junction(path) or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in ("chat_history.txt", "references.csv", "AGENTS.md", "CLAUDE.md", ".solar-request.txt"):
                continue
            if relative.startswith((".agents/", ".claude/", ".codex/")):
                continue
            if path.stat().st_size > limit:
                raise ValueError(f"Output file exceeds upload limit: {relative}")
            count += 1
            if count > 2000:
                raise ValueError("Workspace exceeds 2000 files; outputs retained for local recovery")
            virtual = relative if "/".join(relative.split("/")[:2]) in FOLDERS else "codex/files/" + relative
            data = path.read_bytes()
            existing = chat.files.filter(path_hash=digest(virtual.casefold(), f"path:{chat.id}")).first()
            if existing and read_file(existing) == data:
                continue
            put_file(chat, virtual, data)
    sync_manifests(chat)
    # Only this generated UUID directory beneath runtime may be removed.
    expected = config()["data_dir"] / "runtime"
    if root.resolve().parent != expected.resolve() or root.name != str(chat.id):
        raise ValueError("Workspace cleanup boundary failed")
    shutil.rmtree(root)


def image_preview(record):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with Image.open(io.BytesIO(read_file(record))) as image:
        image.thumbnail((1600, 1600))
        result = io.BytesIO()
        image.convert("RGB").save(result, "JPEG", quality=85)
        return result.getvalue()
