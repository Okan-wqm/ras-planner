import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# For localhost use only. Set RASPLAN_SECRET_KEY in the environment (and change DEBUG) before
# deploying to a server; the fallback below is for local development only.
SECRET_KEY = os.environ.get("RASPLAN_SECRET_KEY", "dev-only-rasplan-local")
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# RASPlanner holds its user's work in a local database: no framing, no content-type sniffing
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # last, so it sees the request as it arrived: a localhost-only tool must not serve other machines
    "rasplan.localonly.LocalOnlyMiddleware",
]

ROOT_URLCONF = "rasplan.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "planner.context.workflow",
    ]},
}]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Istanbul"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# console logging so a failed calculation (a lamp going red) leaves a trace instead of locking the site
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
