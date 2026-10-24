from datetime import datetime, timedelta
from functools import wraps
import io
import json
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import zipfile

from dateutil.relativedelta import relativedelta
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from solar.config import config, save_config
from solar.crypto import cipher
from solar.core.auth import clear_user_attempts, consume_login_attempt
from solar.core.cron import next_cron, parse as parse_cron
from solar.core.models import Agent, Audit, Chat, Project, Provider, Run, Schedule, Skill, StoredFile, User
from solar.core.providers import (APPROVALS, EFFORTS, PERMISSIONS, default_providers, effective_permission,
                                  model_capabilities, provider_models, validate_endpoint, validate_effort)
from solar.core.runner import cancel_run, queue_run
from solar.core import vault


def response(data=None, status=200):
    return JsonResponse(data if data is not None else {"ok": True}, status=status, safe=isinstance(data, dict) or data is None)


def api(methods=("GET",), admin=False, public=False):
    def decorator(function):
        @wraps(function)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                result = response({"error": "Method not allowed"}, 405)
                result["Allow"] = ", ".join(methods)
                return result
            if not public and not request.user.is_authenticated:
                return response({"error": "Sign in to continue"}, 401)
            if admin and not request.user.is_admin:
                return response({"error": "Administrator access required"}, 403)
            try:
                return function(request, *args, **kwargs)
            except (Http404, ObjectDoesNotExist):
                return response({"error": "Not found"}, 404)
            except PermissionError as exc:
                return response({"error": str(exc)}, 403)
            except (ValueError, KeyError, TypeError, ValidationError, ZoneInfoNotFoundError, zipfile.BadZipFile) as exc:
                message = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                return response({"error": message or "Invalid request"}, 400)
            except IntegrityError:
                return response({"error": "This record already exists or conflicts with an active operation"}, 409)
        return wrapped
    return decorator


def body(request):
    if len(request.body) > 1024**2:
        raise ValueError("Request is too large")
    data = json.loads(request.body or b"{}")
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def text(data, name, default="", limit=1000, required=False):
    value = data.get(name, default)
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"Invalid {name} (maximum {limit} characters)")
    if required and not value.strip():
        raise ValueError(f"{name} is required")
    return value if name in {"password", "current_password", "api_key", "content", "instructions", "prompt", "message"} else value.strip()


def boolean(data, name, default=False):
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def integer(data, name, default, low, high):
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def own(model, user, pk):
    try:
        return get_object_or_404(model, pk=uuid.UUID(str(pk)), owner=user)
    except ValueError as exc:
        raise Http404 from exc


def owned_ids(model, user, ids):
    if not isinstance(ids, list) or len(ids) > 50:
        raise ValueError("Choose at most 50 resources")
    uuids = {uuid.UUID(str(value)) for value in ids}
    rows = list(model.objects.filter(owner=user, id__in=uuids))
    if len(rows) != len(uuids):
        raise Http404
    return rows


def require_password(request, data):
    if not request.user.check_password(text(data, "current_password", limit=512, required=True)):
        raise PermissionError("Current password is incorrect")


def serialize_user(user):
    return {"id": str(user.id), "username": user.username, "is_admin": user.is_admin,
            "is_active": user.is_active, "host_access": user.host_access, "remote_access": user.remote_access,
            "must_change_password": user.must_change_password}


def serialize_provider(provider):
    cfg = json.loads(provider.configuration)
    return {"id": str(provider.id), "name": provider.name, "kind": provider.kind, "model": provider.model,
            "base_url": cfg.get("base_url", ""), "has_api_key": bool(cfg.get("api_key")),
            "has_login": bool(cfg.get("auth_json")), "tools": cfg.get("tools", True),
            "vision": cfg.get("vision", False), "reasoning_effort": cfg.get("reasoning_effort", False)}


def serialize_agent(agent):
    return {"id": str(agent.id), "name": agent.name, "role": agent.role, "instructions": agent.instructions,
            "permission": agent.permission, "allow_shell": agent.allow_shell, "allow_network": agent.allow_network,
            "skills": [str(pk) for pk in agent.skills.values_list("id", flat=True)],
            "delegates": [str(pk) for pk in agent.delegates.values_list("id", flat=True)]}


def serialize_chat(chat):
    active = chat.runs.filter(status__in=Run.ACTIVE).first()
    return {"id": str(chat.id), "title": chat.title, "provider_id": str(chat.provider_id),
            "provider_name": chat.provider.name, "provider_kind": chat.provider.kind,
            "agent_id": str(chat.agent_id) if chat.agent_id else None, "model": chat.model,
            "effort": chat.effort, "permission": chat.permission, "archived": chat.archived,
            "approval": chat.approval, "allow_shell": chat.allow_shell, "allow_network": chat.allow_network,
            "windows_sandbox": chat.windows_sandbox, "effective_permission": effective_permission(chat),
            "project_id": str(chat.project_id) if chat.project_id else None,
            "project_name": chat.project.name if chat.project_id else "",
            "workspace": chat.project.root_path if chat.project_id and chat.owner.host_access else "",
            "skills": [str(pk) for pk in chat.skills.values_list("id", flat=True)],
            "created_at": chat.created_at, "updated_at": chat.updated_at,
            "active_run": serialize_run(active) if active else None,
            "directory": f"chat_{chat.created_at:%Y-%m-%d}_{chat.id}"}


def serialize_run(run):
    return {"id": str(run.id), "chat_id": str(run.chat_id), "mode": run.mode, "status": run.status,
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
            "error": run.error}


def serialize_schedule(schedule):
    return {"id": str(schedule.id), "name": schedule.name, "prompt": schedule.prompt,
            "provider_id": str(schedule.provider_id), "agent_id": str(schedule.agent_id) if schedule.agent_id else None,
            "model": schedule.model, "effort": schedule.effort, "permission": schedule.permission,
            "kind": schedule.kind, "interval_seconds": schedule.interval_seconds, "cron": schedule.cron,
            "timezone": schedule.timezone, "starts_at": schedule.starts_at, "ends_at": schedule.ends_at,
            "max_runs": schedule.max_runs, "run_count": schedule.run_count, "next_run": schedule.next_run,
            "enabled": schedule.enabled, "chat_id": str(schedule.chat_id) if schedule.chat_id else None,
            "project_id": str(schedule.project_id) if schedule.project_id else None,
            "approval": schedule.approval, "allow_shell": schedule.allow_shell, "allow_network": schedule.allow_network,
            "windows_sandbox": schedule.windows_sandbox,
            "history": [{"due_at": h.due_at, "run": serialize_run(h.run) if h.run_id else None}
                        for h in schedule.history.select_related("run")[:20]]}


@ensure_csrf_cookie
def index(request):
    return render(request, "index.html", {"csrf": get_token(request)})


def csrf_failure(request, reason=""):
    return response({"error": "Security token expired or request origin is invalid. Reload the page and try again."}, 403)


@api(public=True)
def me(request):
    result = {"user": serialize_user(request.user) if request.user.is_authenticated else None,
              "csrf": get_token(request), "workspace_controls_version": 3}
    if request.user.is_authenticated:
        result["limits"] = {"max_file_mb": config()["storage"]["max_file_mb"], "quota_mb": config()["storage"]["quota_mb"]}
        result["storage_bytes"] = StoredFile.objects.filter(chat__owner=request.user).aggregate(n=Sum("size"))["n"] or 0
        result["desktop_enabled"] = config()["desktop"]["enabled"]
    return response(result)


@api(("POST",), public=True)
def sign_in(request):
    data = body(request)
    username = text(data, "username", required=True, limit=128)
    password = text(data, "password", required=True, limit=512)
    if not consume_login_attempt(username, request.META.get("REMOTE_ADDR", "unknown")):
        return response({"error": "Too many sign-in attempts. Try again in 15 minutes."}, 429)
    user = authenticate(request, username=username, password=password)
    if not user:
        return response({"error": "Username or password is incorrect"}, 401)
    login(request, user)
    clear_user_attempts(username)
    Audit.objects.create(actor=user, action="login")
    return response({"user": serialize_user(user), "csrf": get_token(request)})


@api(("POST",))
def sign_out(request):
    from solar.core.desktop import desktop
    from solar.core.admin_terminal import shells
    shells.stop_for_session(request.session.session_key)
    desktop.stop_for_session(request.session.session_key)
    logout(request)
    return response({"csrf": get_token(request)})


@api(("POST",))
def password_change(request):
    data = body(request)
    require_password(request, data)
    password = text(data, "password", limit=512, required=True)
    validate_password(password, request.user)
    request.user.set_password(password)
    request.user.must_change_password = False
    request.user.save(update_fields=["password", "must_change_password"])
    from solar.core.admin_terminal import shells
    shells.stop_for_user(request.user.id)
    update_session_auth_hash(request, request.user)
    Audit.objects.create(actor=request.user, action="password_changed")
    if config()["bootstrap"].get("user_id") == str(request.user.id):
        config()["bootstrap"].pop("password_encrypted", None)
        config()["bootstrap"]["password_hash"] = request.user.password
        save_config(config())
    return response({"user": serialize_user(request.user), "csrf": get_token(request)})


@api(("GET", "POST"), admin=True)
def users(request):
    if request.method == "GET":
        return response({"users": [serialize_user(user) for user in User.objects.order_by("created_at")]})
    data = body(request)
    require_password(request, data)
    username = text(data, "username", required=True, limit=128)
    password = text(data, "password", required=True, limit=512)
    validate_password(password)
    if len(username) < 3 or any(ord(c) < 32 for c in username):
        raise ValueError("Username must contain at least 3 printable characters")
    with transaction.atomic():
        user = User.objects.create_user(username, password, is_admin=boolean(data, "is_admin"),
                                        host_access=boolean(data, "host_access"), remote_access=boolean(data, "remote_access"),
                                        is_active=boolean(data, "is_active", True))
        default_providers(user)
        Audit.objects.create(actor=request.user, action="user_created", details=str(user.id))
    return response(serialize_user(user), 201)


@api(("PATCH", "DELETE"), admin=True)
def user_detail(request, user_id):
    data = body(request)
    require_password(request, data)
    with transaction.atomic():
        user = get_object_or_404(User, pk=user_id)
        active = boolean(data, "is_active", user.is_active) if request.method == "PATCH" else False
        admin = boolean(data, "is_admin", user.is_admin)
        if user.id == request.user.id and (not active or not admin):
            raise ValueError("Use a different administrator account to remove or demote this account")
        if user.is_admin and user.is_active and (not admin or not active) and User.objects.filter(is_admin=True, is_active=True).count() <= 1:
            raise ValueError("At least one active administrator must remain")
        user.is_active, user.is_admin = active, admin
        user.host_access = boolean(data, "host_access", user.host_access)
        user.remote_access = boolean(data, "remote_access", user.remote_access)
        if data.get("password"):
            password = text(data, "password", limit=512)
            validate_password(password, user)
            user.set_password(password)
            user.must_change_password = True
        if request.method == "DELETE":
            # Removal revokes access; encrypted records remain recoverable until local retention cleanup.
            user.set_unusable_password()
            user.host_access = user.remote_access = False
        user.save()
        if not user.is_active:
            Schedule.objects.filter(owner=user).update(enabled=False)
        Audit.objects.create(actor=request.user, action="user_removed" if request.method == "DELETE" else "user_updated", details=str(user.id))
    if not user.is_active or not user.host_access:
        for run in Run.objects.filter(chat__owner=user, status__in=Run.ACTIVE).exclude(chat__provider__kind="openai"):
            cancel_run(run)
    if not user.is_active or not user.is_admin or not user.host_access or data.get("password"):
        from solar.core.admin_terminal import shells
        shells.stop_for_user(user.id)
    return response(serialize_user(user))


@api(("GET", "POST"))
def providers(request):
    if request.method == "GET":
        return response({"providers": [serialize_provider(p) for p in Provider.objects.filter(owner=request.user).order_by("created_at")],
                         "allowed_origins": config()["providers"]["allowed_api_origins"]})
    data = body(request)
    kind = text(data, "kind", required=True)
    if kind not in ("codex", "claude", "openai"):
        raise ValueError("Unknown provider type")
    provider = Provider(owner=request.user, kind=kind)
    update_provider(provider, data)
    return response(serialize_provider(provider), 201)


def update_provider(provider, data):
    provider.name = text(data, "name", provider.name or "Provider", limit=80, required=True)
    provider.model = text(data, "model", provider.model, limit=160)
    cfg = json.loads(provider.configuration or "{}")
    if provider.kind == "openai":
        cfg["base_url"] = validate_endpoint(text(data, "base_url", cfg.get("base_url", ""), required=True))
    if "api_key" in data and data["api_key"]:
        cfg["api_key"] = text(data, "api_key", limit=4096)
    if boolean(data, "clear_api_key"):
        cfg.pop("api_key", None)
        cfg.pop("auth_json", None)
    for name, default in (("tools", True), ("vision", False), ("reasoning_effort", False)):
        cfg[name] = boolean(data, name, cfg.get(name, default))
    provider.configuration = json.dumps(cfg)
    provider.save()


@api(("PATCH", "DELETE"))
def provider_detail(request, provider_id):
    provider = own(Provider, request.user, provider_id)
    if request.method == "DELETE":
        if Chat.objects.filter(provider=provider).exists() or Schedule.objects.filter(provider=provider).exists():
            raise ValueError("Provider is used by chats or schedules")
        provider.delete()
        return response()
    update_provider(provider, body(request))
    return response(serialize_provider(provider))


@api()
def models(request, provider_id):
    provider = own(Provider, request.user, provider_id)
    try:
        return response({"models": provider_models(provider), "capabilities": model_capabilities(provider)})
    except Exception:
        return response({"models": [], "error": "Model list unavailable. Check provider setup, or enter a model ID manually."})


@api(("GET", "POST"))
def agents(request):
    if request.method == "GET":
        return response({"agents": [serialize_agent(a) for a in Agent.objects.filter(owner=request.user)]})
    agent = Agent(owner=request.user)
    update_agent(agent, body(request), request.user)
    return response(serialize_agent(agent), 201)


def update_agent(agent, data, user):
    agent.name = text(data, "name", agent.name, required=True, limit=80)
    agent.role = text(data, "role", agent.role, required=True, limit=200)
    agent.instructions = text(data, "instructions", agent.instructions, required=True, limit=40_000)
    permission = text(data, "permission", agent.permission or "read-only")
    if permission not in PERMISSIONS:
        raise ValueError("Invalid permission profile")
    agent.permission = permission
    agent.allow_shell = boolean(data, "allow_shell", agent.allow_shell)
    agent.allow_network = boolean(data, "allow_network", agent.allow_network)
    if (agent.allow_shell or agent.allow_network or agent.permission == "danger-full-access") and not user.host_access:
        raise PermissionError("Shell and network tools require host execution permission")
    skills = owned_ids(Skill, user, data.get("skills", []))
    delegates = owned_ids(Agent, user, data.get("delegates", []))
    if any(a.id == agent.id for a in delegates):
        raise ValueError("An agent cannot delegate to itself")
    with transaction.atomic():
        agent.save()
        agent.skills.set(skills)
        agent.delegates.set(delegates)


@api(("PATCH", "DELETE"))
def agent_detail(request, agent_id):
    agent = own(Agent, request.user, agent_id)
    if Run.objects.filter(chat__agent=agent, status__in=Run.ACTIVE).exists():
        raise ValueError("Stop this agent's active runs before changing it")
    if request.method == "DELETE":
        if Chat.objects.filter(agent=agent).exists() or Schedule.objects.filter(agent=agent).exists():
            raise ValueError("Unassign this role from its chats and schedules before removing it")
        agent.delete()
        return response()
    update_agent(agent, body(request), request.user)
    return response(serialize_agent(agent))


def serialize_skill(skill, content=False):
    result = {"id": str(skill.id), "name": skill.name, "description": skill.description,
              "has_bundle": bool(skill.bundle), "updated_at": skill.updated_at}
    if content:
        result["content"] = skill.content
    return result


@api(("GET", "POST"))
def skills(request):
    if request.method == "GET":
        return response({"skills": [serialize_skill(s, True) for s in Skill.objects.filter(owner=request.user)]})
    if request.FILES:
        upload = request.FILES.get("file")
        if not upload or upload.size > 10 * 1024**2:
            raise ValueError("Upload a SKILL.md or ZIP bundle up to 10 MB")
        raw = upload.read()
        skill = Skill(owner=request.user)
        if upload.name.lower().endswith(".zip"):
            members = vault.zip_members(raw, max_total=10 * 1024**2)
            matches = [(path, value) for path, value in members if path.endswith("SKILL.md")]
            if len(matches) != 1:
                raise ValueError("A skill bundle must contain exactly one SKILL.md")
            path, content = matches[0]
            prefix = path[:-len("SKILL.md")]
            normalized = io.BytesIO()
            with zipfile.ZipFile(normalized, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, value in members:
                    if not name.startswith(prefix):
                        raise ValueError("All skill resources must be beneath the SKILL.md folder")
                    archive.writestr(name[len(prefix):], value)
            skill.bundle = cipher(f"skill:{skill.id}").encrypt(normalized.getvalue())
            raw = content
        skill.content = raw.decode("utf-8-sig")
        if len(skill.content) > 100_000:
            raise ValueError("SKILL.md exceeds 100,000 characters")
        match = re.search(r"(?m)^name:\s*([^\r\n]+)", skill.content)
        skill.name = match.group(1).strip("\"'")[:80] if match else upload.name.rsplit(".", 1)[0][:80]
        skill.description = "Imported skill. Review its instructions before enabling it."
        skill.save()
    else:
        data = body(request)
        skill = Skill.objects.create(owner=request.user, name=text(data, "name", required=True, limit=80),
                                     description=text(data, "description", limit=1000),
                                     content=text(data, "content", required=True, limit=100_000))
    return response(serialize_skill(skill, True), 201)


@api(("GET", "PATCH", "DELETE"))
def skill_detail(request, skill_id):
    skill = own(Skill, request.user, skill_id)
    if request.method == "DELETE":
        skill.delete()
        return response()
    if request.method == "PATCH":
        data = body(request)
        skill.name = text(data, "name", skill.name, required=True, limit=80)
        skill.description = text(data, "description", skill.description)
        skill.content = text(data, "content", skill.content, required=True, limit=100_000)
        skill.save()
    if request.GET.get("download"):
        if skill.bundle:
            members = vault.zip_members(cipher(f"skill:{skill.id}").decrypt(bytes(skill.bundle)), max_total=10 * 1024**2)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, content in members:
                    if path != "SKILL.md":
                        archive.writestr(path, content)
                archive.writestr("SKILL.md", skill.content)
            return attachment(output.getvalue(), f"skill-{skill.id}.zip", "application/zip")
        return attachment(skill.content.encode("utf-8"), "SKILL.md", "text/markdown")
    return response(serialize_skill(skill, True))


EXECUTION_FIELDS = ("provider_id", "agent_id", "project_id", "model", "effort", "permission", "approval",
                    "allow_shell", "allow_network", "windows_sandbox")


def apply_execution_controls(item, data, user, scheduled=False):
    if "project_id" in data:
        if data["project_id"]:
            from solar.core.projects import require_host, project_root
            require_host(user)
            item.project = own(Project, user, data["project_id"])
            project_root(item.project, user)
        else:
            item.project = None
    item.approval = text(data, "approval", item.approval)
    item.allow_shell = boolean(data, "allow_shell", item.allow_shell)
    item.allow_network = boolean(data, "allow_network", item.allow_network)
    item.windows_sandbox = text(data, "windows_sandbox", item.windows_sandbox)
    if item.approval not in APPROVALS or item.windows_sandbox not in ("unelevated", "elevated"):
        raise ValueError("Invalid approval or Windows sandbox mode")
    if (item.permission == "danger-full-access" or item.allow_network or item.windows_sandbox == "elevated") and not user.host_access:
        raise PermissionError("Full access and host policy settings require host execution permission")
    if scheduled and item.approval == "on-request":
        raise ValueError("Automations cannot wait for interactive approval; choose Never ask or Automatic review")
    if item.approval == "auto-review" and (item.provider.kind != "codex" or effective_permission(item) != "workspace-write"):
        raise ValueError("Automatic review requires Codex and workspace-write permission, including the agent role")
    if item.provider.kind == "openai" and item.approval not in ("default", "never"):
        raise ValueError("API tools run under the selected role permissions; interactive approvals require a CLI provider")


def apply_chat_settings(chat, data, user):
    if "provider_id" in data:
        chat.provider = own(Provider, user, data["provider_id"])
    if "agent_id" in data:
        chat.agent = own(Agent, user, data["agent_id"]) if data["agent_id"] else None
    chat.title = text(data, "title", chat.title or "Untitled session", required=True, limit=200)
    chat.model = text(data, "model", chat.model or chat.provider.model, limit=160) or chat.provider.model
    chat.effort = text(data, "effort", chat.effort or "medium")
    chat.permission = text(data, "permission", chat.permission or "read-only")
    if chat.effort not in EFFORTS or chat.permission not in PERMISSIONS:
        raise ValueError("Invalid reasoning effort or permission profile")
    validate_effort(chat.provider.kind, chat.effort)
    apply_execution_controls(chat, data, user)
    chat.archived = boolean(data, "archived", chat.archived)
    if chat.archived and chat.pk and chat.runs.filter(status__in=Run.ACTIVE).exists():
        raise ValueError("Stop the run before archiving this chat")
    skill_rows = owned_ids(Skill, user, data.get("skills", [])) if "skills" in data else None
    chat.updated_at = timezone.now()
    chat.save()
    if skill_rows is not None:
        chat.skills.set(skill_rows)
    Schedule.objects.filter(chat=chat).update(**{field: getattr(chat, field) for field in EXECUTION_FIELDS})
    if chat.archived:
        Schedule.objects.filter(chat=chat).update(enabled=False)


@api(("GET", "POST"))
def chats(request):
    if request.method == "GET":
        offset = max(0, int(request.GET.get("offset", 0)))
        query = request.GET.get("q", "").strip().casefold()[:200]
        rows = Chat.objects.filter(owner=request.user, archived=request.GET.get("archived") == "true")
        if query:
            matches = [chat.id for chat in rows.only("id", "title").iterator() if query in chat.title.casefold()]
            rows = rows.filter(pk__in=matches)
        total = rows.count()
        items = rows.select_related("provider")[offset:offset + 200]
        return response({"chats": [serialize_chat(chat) for chat in items], "total": total,
                         "next_offset": offset + 200 if offset + 200 < total else None})
    data = body(request)
    chat = Chat(owner=request.user, provider=own(Provider, request.user, data["provider_id"]))
    apply_chat_settings(chat, data, request.user)
    vault.sync_manifests(chat)
    return response(serialize_chat(chat), 201)


@api(("GET", "PATCH"))
def chat_detail(request, chat_id):
    chat = own(Chat, request.user, chat_id)
    if request.method == "PATCH":
        data = body(request)
        if chat.runs.filter(status__in=Run.ACTIVE).exists():
            raise ValueError("Stop the active run before changing session settings")
        if "provider_id" in data and data["provider_id"] != str(chat.provider_id) and chat.native_state:
            raise ValueError("Create a new chat to change providers after a native session")
        if Schedule.objects.filter(chat=chat).exists() and data.get("approval") == "on-request":
            raise ValueError("This is an automation session; interactive approvals would block its scheduled runs")
        apply_chat_settings(chat, data, request.user)
    return response(serialize_chat(chat))


@api()
def events(request, chat_id):
    chat = own(Chat, request.user, chat_id)
    after = max(0, int(request.GET.get("after", 0)))
    rows = list(chat.events.filter(id__gt=after).order_by("id")[:300])
    return response({"events": [{"id": e.id, "run_id": str(e.run_id) if e.run_id else None,
                                "role": e.role, "kind": e.kind, "content": e.content, "created_at": e.created_at} for e in rows],
                     "cursor": rows[-1].id if rows else after, "has_more": len(rows) == 300,
                     "active_run": next((serialize_run(r) for r in chat.runs.filter(status__in=Run.ACTIVE)), None)})


@api(("POST",))
def send_message(request, chat_id):
    chat = own(Chat, request.user, chat_id)
    data = body(request)
    prompt = text(data, "message", required=True, limit=100_000)
    if prompt.startswith("/"):
        return slash_command(chat, prompt, request.user)
    run = queue_run(chat, prompt)
    return response({"run": serialize_run(run), "native": run.mode == "native"}, 202)


def slash_command(chat, prompt, user):
    command, _, argument = prompt.partition(" ")
    argument = argument.strip()
    if command == "/stop":
        for run in chat.runs.filter(status__in=Run.ACTIVE):
            cancel_run(run)
        return response({"notice": "Stop requested"})
    if command == "/native":
        return response({"native": True, "run": serialize_run(queue_run(chat, mode="native"))}, 202)
    if command in ("/help", "/status", "/skills"):
        notices = {
            "/help": "/model <ID>, /effort <level>, /permissions read-only|workspace-write|danger-full-access, "
                     "/approvals default|on-request|never|auto-review, /workspace <project|none>, /agent <name|none>, "
                     "/skill <name>, /status, /skills, /stop, /plan, /review, /compact, /native. "
                     "Use Native terminal for all commands supported by the installed Codex or Claude CLI.",
            "/status": f"{chat.provider.name} · model {chat.model or 'provider default'} · {chat.effort} · {effective_permission(chat)} · approvals {chat.approval}",
            "/skills": "\n".join(s.name for s in chat.skills.all()) or "No chat skills enabled",
        }
        return response({"notice": notices[command]})
    if chat.runs.filter(status__in=Run.ACTIVE).exists():
        raise ValueError("Stop the current run before changing settings")
    if command == "/model":
        if not argument or len(argument) > 160:
            raise ValueError("Usage: /model <model ID>")
        chat.model = argument
    elif command == "/effort":
        if argument not in EFFORTS:
            raise ValueError("Effort: minimal, low, medium, high, xhigh, max, ultra (model support varies)")
        validate_effort(chat.provider.kind, argument)
        chat.effort = argument
    elif command == "/permissions":
        if argument not in PERMISSIONS:
            raise ValueError("Usage: /permissions read-only|workspace-write|danger-full-access")
        chat.permission = argument
    elif command == "/approvals":
        if argument not in APPROVALS:
            raise ValueError("Usage: /approvals default|on-request|never|auto-review")
        if argument == "on-request" and Schedule.objects.filter(chat=chat).exists():
            raise ValueError("Interactive approvals cannot be enabled for an automation session")
        chat.approval = argument
    elif command in ("/workspace", "/project"):
        if argument.lower() == "none":
            chat.project = None
        else:
            from solar.core.projects import require_host, project_root
            require_host(user)
            matches = [p for p in Project.objects.filter(owner=user) if p.name.casefold() == argument.casefold() or str(p.id) == argument]
            if len(matches) != 1:
                raise ValueError("Use an exact project name or ID from Projects")
            project_root(matches[0], user)
            chat.project = matches[0]
    elif command == "/plan":
        chat.permission = "read-only"
    elif command == "/agent":
        if argument.lower() == "none":
            chat.agent = None
        else:
            matches = [a for a in Agent.objects.filter(owner=user) if a.name.casefold() == argument.casefold() or str(a.id) == argument]
            if len(matches) != 1:
                raise ValueError("Use an exact, unique agent name or ID")
            chat.agent = matches[0]
    elif command == "/skill":
        matches = [s for s in Skill.objects.filter(owner=user) if s.name.casefold() == argument.casefold() or str(s.id) == argument]
        if len(matches) != 1:
            raise ValueError("Use an exact, unique skill name or ID")
        chat.skills.add(matches[0])
    elif command in ("/review", "/compact"):
        message = "Review the files in this workspace and report issues without modifying files." if command == "/review" else "Summarize this conversation, key decisions, files, and remaining work concisely."
        return response({"run": serialize_run(queue_run(chat, message + " " + argument))}, 202)
    else:
        raise ValueError("Unknown web command. Use /help, or open Native terminal for the provider's full command set.")
    apply_execution_controls(chat, {}, user)
    chat.save()
    Schedule.objects.filter(chat=chat).update(**{field: getattr(chat, field) for field in EXECUTION_FIELDS})
    return response({"notice": f"Updated {command[1:]}", "chat": serialize_chat(chat)})


@api(("POST",))
def native_start(request, chat_id):
    chat = own(Chat, request.user, chat_id)
    return response({"run": serialize_run(queue_run(chat, mode="native"))}, 202)


@api(("GET",))
def runs(request):
    return response({"runs": [serialize_run(run) for run in Run.objects.filter(chat__owner=request.user).order_by("-created_at")[:100]]})


@api(("POST",))
def run_stop(request, run_id):
    run = get_object_or_404(Run, pk=run_id, chat__owner=request.user)
    cancel_run(run)
    return response()


@api(("GET", "POST"))
def files(request, chat_id):
    chat = own(Chat, request.user, chat_id)
    if request.method == "POST":
        if chat.runs.filter(status__in=Run.ACTIVE).exists():
            raise ValueError("Stop the active run before uploading files")
        uploads = request.FILES.getlist("files")
        paths = request.POST.getlist("paths")
        if not uploads or len(uploads) > 200 or (paths and len(paths) != len(uploads)):
            raise ValueError("Provide 1 to 200 files and matching relative paths")
        validated = []
        for i, upload in enumerate(uploads):
            path = vault.safe_path(paths[i] if paths else upload.name)
            category = "files"
            if "/" not in path and upload.name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                category = "images"
            elif "/" not in path and upload.name.lower().endswith(".zip"):
                category = "zip_files"
            if upload.size > config()["storage"]["max_file_mb"] * 1024**2:
                raise ValueError("A file exceeds the configured upload limit")
            validated.append((f"user/{category}/{path}", upload))
        for path, upload in validated:
            vault.put_file(chat, path, upload.read())
        vault.sync_manifests(chat)
    return response({"folders": list(vault.FOLDERS), "files": [{"id": str(f.id), "path": f.path, "size": f.size,
                                                                "mime": f.mime, "updated_at": f.updated_at} for f in chat.files.all()],
                     "manifests": ["chat_history.txt", "references.csv"]})


def attachment(data, filename, mime="application/octet-stream"):
    from django.utils.http import content_disposition_header
    result = HttpResponse(data, content_type=mime)
    result["Content-Disposition"] = content_disposition_header(True, filename)
    result["X-Content-Type-Options"] = "nosniff"
    result["Cache-Control"] = "no-store"
    return result


@api(("GET", "DELETE"))
def file_detail(request, file_id):
    file = get_object_or_404(StoredFile, pk=file_id, chat__owner=request.user)
    if request.method == "DELETE":
        if file.chat.runs.filter(status__in=Run.ACTIVE).exists():
            raise ValueError("Stop the run before deleting files")
        chat = file.chat
        vault.delete_file(file)
        vault.sync_manifests(chat)
        return response()
    if request.GET.get("preview") == "true":
        if file.mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise ValueError("Preview is available for raster images only")
        return HttpResponse(vault.image_preview(file), content_type="image/jpeg")
    return attachment(vault.read_file(file), file.path.rsplit("/", 1)[-1])


@api(("POST",))
def file_extract(request, file_id):
    file = get_object_or_404(StoredFile, pk=file_id, chat__owner=request.user)
    if file.chat.runs.filter(status__in=Run.ACTIVE).exists():
        raise ValueError("Stop the run before extracting an archive")
    members = vault.zip_members(vault.read_file(file))
    prefix = "user/files/" + file.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    for path, content in members:
        vault.put_file(file.chat, prefix + "/" + path, content)
    vault.sync_manifests(file.chat)
    return response({"extracted": len(members)})


@api()
def export_chat(request, chat_id):
    chat = own(Chat, request.user, chat_id)
    kind = request.GET.get("kind", "zip")
    if kind == "history":
        return attachment(vault.history_text(chat).encode("utf-8"), "chat_history.txt", "text/plain; charset=utf-8")
    if kind == "references":
        return attachment(vault.references_csv(chat).encode("utf-8-sig"), "references.csv", "text/csv; charset=utf-8")
    return attachment(vault.export_zip(chat, request.GET.get("prefix", "")), f"chat_{chat.id}.zip", "application/zip")


def parse_date(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 timestamp with timezone")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(date):
        raise ValueError(f"{name} must include a timezone offset")
    return date


def update_schedule(schedule, data, user):
    if schedule.chat_id and schedule.chat.runs.filter(status__in=Run.ACTIVE).exists():
        raise ValueError("Stop the automation session's active run before changing its settings")
    schedule.name = text(data, "name", schedule.name, required=True, limit=100)
    schedule.prompt = text(data, "prompt", schedule.prompt, required=True, limit=100_000)
    if "provider_id" in data:
        schedule.provider = own(Provider, user, data["provider_id"])
        if schedule.chat_id and schedule.chat.native_state and schedule.chat.provider_id != schedule.provider_id:
            raise ValueError("This task has a saved provider session. Create a separate automation to change providers.")
    if "agent_id" in data:
        schedule.agent = own(Agent, user, data["agent_id"]) if data["agent_id"] else None
    schedule.model = text(data, "model", schedule.model or schedule.provider.model, limit=160) or schedule.provider.model
    schedule.effort = text(data, "effort", schedule.effort or "medium")
    schedule.permission = text(data, "permission", schedule.permission or "read-only")
    if schedule.effort not in EFFORTS or schedule.permission not in PERMISSIONS:
        raise ValueError("Invalid effort or permission")
    validate_effort(schedule.provider.kind, schedule.effort)
    apply_execution_controls(schedule, data, user, scheduled=True)
    if schedule.provider.kind != "openai" and not user.host_access:
        raise PermissionError("Scheduling CLI providers requires host execution permission")
    schedule.kind = text(data, "kind", schedule.kind or "interval")
    if schedule.kind not in ("once", "interval", "cron"):
        raise ValueError("Choose once, interval, or cron")
    schedule.timezone = text(data, "timezone", schedule.timezone or "UTC", limit=64)
    ZoneInfo(schedule.timezone)
    schedule.interval_seconds = integer(data, "interval_seconds", schedule.interval_seconds or 3600, 60, 366 * 86400)
    schedule.cron = text(data, "cron", schedule.cron or "", limit=100)
    if schedule.kind == "cron":
        parse_cron(schedule.cron)
    if "starts_at" in data or not schedule.next_run:
        schedule.starts_at = parse_date(data["starts_at"], "starts_at") if data.get("starts_at") else timezone.now()
    if data.get("duration_months"):
        months = integer(data, "duration_months", 3, 1, 120)
        schedule.ends_at = schedule.starts_at + relativedelta(months=months)
    elif "ends_at" in data:
        schedule.ends_at = parse_date(data["ends_at"], "ends_at") if data["ends_at"] else None
    if schedule.ends_at and schedule.ends_at <= schedule.starts_at:
        raise ValueError("End must be after start")
    if "max_runs" in data:
        schedule.max_runs = integer(data, "max_runs", 1, 1, 100_000) if data["max_runs"] is not None else None
    schedule.enabled = boolean(data, "enabled", schedule.enabled)
    if schedule.enabled and schedule.max_runs is not None and schedule.run_count >= schedule.max_runs:
        raise ValueError("Increase the run limit before resuming this completed task")
    schedule.next_run = next_cron(schedule.cron, max(schedule.starts_at, timezone.now()) - timedelta(minutes=1), schedule.timezone) if schedule.kind == "cron" else max(schedule.starts_at, timezone.now())
    if schedule.ends_at and schedule.next_run > schedule.ends_at:
        raise ValueError("The schedule has no future occurrence before its end")
    schedule.save()
    if schedule.chat_id:
        Chat.objects.filter(pk=schedule.chat_id).update(**{field: getattr(schedule, field) for field in EXECUTION_FIELDS})


@api(("GET", "POST"))
def schedules(request):
    if request.method == "GET":
        return response({"schedules": [serialize_schedule(s) for s in Schedule.objects.filter(owner=request.user)]})
    data = body(request)
    schedule = Schedule(owner=request.user, provider=own(Provider, request.user, data["provider_id"]))
    update_schedule(schedule, data, request.user)
    return response(serialize_schedule(schedule), 201)


@api(("PATCH", "DELETE"))
def schedule_detail(request, schedule_id):
    schedule = own(Schedule, request.user, schedule_id)
    if request.method == "DELETE":
        schedule.delete()
        return response()
    data = body(request)
    if set(data) == {"enabled"}:
        schedule.enabled = boolean(data, "enabled")
        if schedule.enabled and ((schedule.max_runs is not None and schedule.run_count >= schedule.max_runs)
                                 or (schedule.ends_at and schedule.ends_at <= timezone.now())):
            raise ValueError("Extend the completed schedule before resuming it")
        schedule.save(update_fields=["enabled"])
    else:
        update_schedule(schedule, data, request.user)
    return response(serialize_schedule(schedule))


@api(("GET", "POST", "DELETE"))
def desktop_access(request):
    from solar.core.desktop import desktop
    if not request.user.remote_access:
        raise PermissionError("Remote desktop permission is not enabled for this account")
    if request.method == "POST":
        data = body(request)
        return response(desktop.start(request.user, request.session.session_key, integer(data, "monitor", 1, 0, 16)))
    if request.method == "DELETE":
        desktop.stop_for_session(request.session.session_key)
    return response(desktop.status(request.user, request.session.session_key))
