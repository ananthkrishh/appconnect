"""Django settings for AppConnect.

Everything environment-specific is read from environment variables so the same
settings module is used locally, in Docker and in CI.
"""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "apps.core",
    "apps.employees",
    "apps.marketplace",
    "apps.webhooks",
    "apps.connectors",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.config(
        default="postgres://appconnect:appconnect@localhost:5432/appconnect",
        conn_max_age=60,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "ALLOWED_VERSIONS": ["v1", "v2"],
    "DEFAULT_VERSION": "v2",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.marketplace.authentication.OAuthBearerAuthentication",
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_RATES": {"oauth": "30/min"},
}

# Versions that still work but are scheduled for removal. Responses carry
# `Deprecation` and `Sunset` headers (RFC 8594).
API_DEPRECATED_VERSIONS = {"v1": "Wed, 30 Jun 2027 00:00:00 GMT"}

SPECTACULAR_SETTINGS = {
    "TITLE": "AppConnect API",
    "DESCRIPTION": (
        "Employee records API and app marketplace. Third-party apps install via "
        "OAuth 2.0 and receive HMAC-signed webhooks for hire, update and "
        "termination events."
    ),
    "VERSION": "2.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]",
    "ENUM_NAME_OVERRIDES": {
        "EmployeeStatusEnum": "apps.employees.models.Employee.Status",
        "EmploymentTypeEnum": "apps.employees.models.Employee.EmploymentType",
        "InstallationStatusEnum": "apps.marketplace.models.Installation.Status",
        "DeliveryStatusEnum": "apps.webhooks.models.WebhookDelivery.Status",
    },
}

# --------------------------------------------------------------------------- #
# Celery / Redis
# --------------------------------------------------------------------------- #
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {"webhooks.*": {"queue": "webhooks"}}

# --------------------------------------------------------------------------- #
# OAuth 2.0 (authorization code + PKCE, refresh token rotation)
# --------------------------------------------------------------------------- #
OAUTH_AUTH_CODE_TTL = env_int("OAUTH_AUTH_CODE_TTL", 600)
OAUTH_ACCESS_TOKEN_TTL = env_int("OAUTH_ACCESS_TOKEN_TTL", 3600)
OAUTH_REFRESH_TOKEN_TTL = env_int("OAUTH_REFRESH_TOKEN_TTL", 30 * 24 * 3600)

# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
WEBHOOK_MAX_ATTEMPTS = env_int("WEBHOOK_MAX_ATTEMPTS", 8)
WEBHOOK_BACKOFF_BASE = env_int("WEBHOOK_BACKOFF_BASE", 5)  # seconds
WEBHOOK_BACKOFF_CAP = env_int("WEBHOOK_BACKOFF_CAP", 3600)  # seconds
WEBHOOK_TIMEOUT = float(os.environ.get("WEBHOOK_TIMEOUT", "10"))
WEBHOOK_STALE_AFTER = env_int("WEBHOOK_STALE_AFTER", 900)  # pending this long => requeue

# --------------------------------------------------------------------------- #
# Connector generator (Llama 3 via Ollama)
# --------------------------------------------------------------------------- #
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
