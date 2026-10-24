import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone as django_timezone

from solar.crypto import EncryptedTextField, digest


class UserManager(BaseUserManager):
    def create_user(self, username, password=None, **extra):
        user = self.model(username=username.strip(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra):
        return self.create_user(username, password, is_admin=True, host_access=True, remote_access=True, **extra)


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = EncryptedTextField()
    login_id = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    is_admin = models.BooleanField(default=False)
    host_access = models.BooleanField(default=False)
    remote_access = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=django_timezone.now)
    objects = UserManager()
    USERNAME_FIELD = "login_id"
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        self.login_id = digest(self.username.strip().casefold(), "login")
        super().save(*args, **kwargs)

    def get_username(self):
        return self.username


class Provider(models.Model):
    KINDS = [("codex", "Codex CLI"), ("claude", "Claude Code"), ("openai", "OpenAI compatible API")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    name = EncryptedTextField()
    kind = models.CharField(max_length=12, choices=KINDS)
    configuration = EncryptedTextField(default="{}")
    model = EncryptedTextField(blank=True, default="")
    created_at = models.DateTimeField(default=django_timezone.now)


class Skill(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    name = EncryptedTextField()
    description = EncryptedTextField(blank=True, default="")
    content = EncryptedTextField()
    # ZIP resources are encrypted as a single bounded bundle; never executed on installation.
    bundle = models.BinaryField(blank=True, default=bytes)
    created_at = models.DateTimeField(default=django_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class Agent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    name = EncryptedTextField()
    role = EncryptedTextField()
    instructions = EncryptedTextField()
    permission = models.CharField(max_length=20, default="read-only")
    allow_shell = models.BooleanField(default=False)
    allow_network = models.BooleanField(default=False)
    skills = models.ManyToManyField(Skill, blank=True)
    delegates = models.ManyToManyField("self", symmetrical=False, blank=True)
    created_at = models.DateTimeField(default=django_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    name = EncryptedTextField()
    root_path = EncryptedTextField()
    path_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=django_timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "path_hash"], name="unique_owned_project_path")]


class Chat(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    title = EncryptedTextField(default="Untitled session")
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, null=True, blank=True)
    model = EncryptedTextField(blank=True, default="")
    effort = models.CharField(max_length=10, default="medium")
    permission = models.CharField(max_length=20, default="read-only")
    approval = models.CharField(max_length=20, default="default")
    allow_shell = models.BooleanField(default=True)
    allow_network = models.BooleanField(default=False)
    windows_sandbox = models.CharField(max_length=12, default="unelevated")
    skills = models.ManyToManyField(Skill, blank=True)
    created_at = models.DateTimeField(default=django_timezone.now)
    updated_at = models.DateTimeField(default=django_timezone.now)
    archived = models.BooleanField(default=False)
    native_state = models.BinaryField(blank=True, default=bytes)
    native_started = models.BooleanField(default=False)
    provider_session = EncryptedTextField(blank=True, default="")

    class Meta:
        ordering = ["-updated_at"]


class Run(models.Model):
    ACTIVE = ("queued", "running", "cancelling")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="runs")
    mode = models.CharField(max_length=12, default="chat")
    status = models.CharField(max_length=16, default="queued")
    error = EncryptedTextField(blank=True, default="")
    created_at = models.DateTimeField(default=django_timezone.now)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    process_id = models.PositiveIntegerField(null=True)
    process_started = models.FloatField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["chat"], condition=models.Q(status__in=["queued", "running", "cancelling"]),
            name="one_active_run_per_chat",
        )]


class Event(models.Model):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="events")
    run = models.ForeignKey(Run, on_delete=models.CASCADE, null=True, blank=True)
    role = models.CharField(max_length=16)
    kind = models.CharField(max_length=20, default="message")
    content = EncryptedTextField()
    created_at = models.DateTimeField(default=django_timezone.now)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["chat", "id"])]


class StoredFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="files")
    path = EncryptedTextField()
    path_hash = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    mime = models.CharField(max_length=128, default="application/octet-stream")
    created_at = models.DateTimeField(default=django_timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["chat", "path_hash"], name="unique_virtual_path")]


class Schedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    name = EncryptedTextField()
    prompt = EncryptedTextField()
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, blank=True, null=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, null=True, blank=True)
    chat = models.OneToOneField(Chat, on_delete=models.SET_NULL, null=True, blank=True, related_name="automation")
    model = EncryptedTextField(blank=True, default="")
    effort = models.CharField(max_length=10, default="medium")
    permission = models.CharField(max_length=20, default="read-only")
    approval = models.CharField(max_length=20, default="never")
    allow_shell = models.BooleanField(default=True)
    allow_network = models.BooleanField(default=False)
    windows_sandbox = models.CharField(max_length=12, default="unelevated")
    kind = models.CharField(max_length=12, default="interval")
    interval_seconds = models.PositiveIntegerField(default=3600)
    cron = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    starts_at = models.DateTimeField(default=django_timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    max_runs = models.PositiveIntegerField(null=True, blank=True)
    run_count = models.PositiveIntegerField(default=0)
    next_run = models.DateTimeField()
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=django_timezone.now)


class ScheduleRun(models.Model):
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name="history")
    due_at = models.DateTimeField()
    run = models.ForeignKey(Run, on_delete=models.SET_NULL, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["schedule", "due_at"], name="one_scheduled_occurrence")]
        ordering = ["-due_at"]


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    attempts = models.PositiveIntegerField(default=0)
    window_at = models.DateTimeField(default=django_timezone.now)


class Audit(models.Model):
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    details = EncryptedTextField(blank=True, default="")
    created_at = models.DateTimeField(default=django_timezone.now)
