import base64
from pathlib import Path

from solar.config import config, local_hosts
from solar.crypto import derive_key

CFG = config()
BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = base64.urlsafe_b64encode(derive_key("django-secret")).decode() + "solar-private-installation"
DEBUG = False
ALLOWED_HOSTS = [f"[{host}]" if ":" in host and not host.startswith("[") else host
                 for host in local_hosts() + CFG["server"]["allowed_hosts"]]
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.staticfiles", "solar.core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "solar.core.middleware.SecurityHeadersMiddleware",
]
ROOT_URLCONF = "solar.urls"
ASGI_APPLICATION = "solar.asgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
    ]},
}]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": CFG["data_dir"] / "solar.sqlite3",
    "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
}}
AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = ["solar.core.auth.PrivateBackend"]
PASSWORD_HASHERS = ["solar.core.hashers.BoundedArgon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_COOKIE_NAME = "solar_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = CFG["server"]["tls"]
SESSION_COOKIE_AGE = 43200
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_NAME = "solar_csrf"
CSRF_COOKIE_SECURE = CFG["server"]["tls"]
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_FAILURE_VIEW = "solar.core.views.csrf_failure"
SECURE_SSL_REDIRECT = CFG["server"]["tls"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000 if CFG["server"]["tls"] else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "no-referrer"
X_FRAME_OPTIONS = "DENY"
STATIC_URL = "/static/"
STATIC_ROOT = CFG["data_dir"] / "static"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_TEMP_DIR = str(CFG["data_dir"] / "runtime")
DATA_UPLOAD_MAX_NUMBER_FILES = 200
DATA_UPLOAD_MAX_NUMBER_FIELDS = 500
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGGING = {"version": 1, "disable_existing_loggers": False,
           "handlers": {"console": {"class": "logging.StreamHandler"}},
           "root": {"handlers": ["console"], "level": "WARNING"}}
