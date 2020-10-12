"""
Django settings for matshare.

For more information on this file, see
https://docs.djangoproject.com/en/3.0/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.0/ref/settings/
"""

import os
import urllib.parse

from django.conf.global_settings import LANGUAGES as AVAILABLE_DJANGO_LANGUAGES
from django.core.exceptions import ImproperlyConfigured
import environ

from matshare.utils import parse_ldap_group_query_string


env = environ.Env()
# Project root is two directory levels up
root = environ.Path(__file__) - 2


DEBUG = env.bool("MS_DEBUG", False)

SECRET_KEY = env.str("MS_SECRET_KEY", "")
if not SECRET_KEY:
    # Provide default secret key in debug mode
    if DEBUG:
        SECRET_KEY = "unsecure"
    else:
        raise ImproperlyConfigured(
            "MS_SECRET_KEY has to be configured when running in production mode"
        )

# These will receive error reports
ADMINS = [(email, email) for email in env.tuple("MS_ERROR_EMAILS", default=())]

# MatShare is guarded by nginx, which passes the original Host header
# through. Additionally, uWSGI rewrites REMOTE_ADDR and wsgi.url_scheme according
# to X-Forwarded-For and X-Forwarded-Proto, so that we don't have to deal with all
# this here.
ALLOWED_HOSTS = ["*"]


# Application definition

# Order matters
INSTALLED_APPS = [
    "django.contrib.admin.apps.SimpleAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Object-level permissions
    "rules",
    # Clever full-text model search
    "watson",
    # Form field displaying
    "widget_tweaks",
    # And, finally, the main app
    "matshare",
]

# Order matters
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "matshare.middleware.AccountBasedLocalizationMiddleware",
    "matshare.middleware.ConsentMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "matshare.urls"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

SESSION_COOKIE_NAME = "ms_session"
# Logout after some time of inactivity
SESSION_COOKIE_AGE = env.int("MS_SESSION_EXPIRATION_SECS", 3600)
assert SESSION_COOKIE_AGE >= 0
# Without this, the validity of sessions would never be reset and we hence couldn't
# make them that short-lived and wouldn't get auto-logout behavior, even though
# it means a database update on every single request... maybe caching is an option
# post MVP
SESSION_SAVE_EVERY_REQUEST = True

AUTH_USER_MODEL = "matshare.User"

AUTHENTICATION_BACKENDS = [
    # This is the backend that enforces all our user permissions
    "rules.permissions.ObjectPermissionBackend",
    # Use this customized subclass of django.contrib.auth.backends.ModelBackend for
    # authentication only, not for authorization
    "matshare.auth.MatShareModelBackend",
]
# Allow authenticating users via LDAP
if env.bool("MS_AUTH_LDAP", False):
    AUTHENTICATION_BACKENDS.append("matshare.auth.MatShareLDAPBackend")
    AUTH_LDAP_SERVER_URI = env.str("MS_AUTH_LDAP_SERVER_URI")
    AUTH_LDAP_USER_DN_TEMPLATE = env.str("MS_AUTH_LDAP_USER_DN_TEMPLATE")
    AUTH_LDAP_REQUIRE_GROUP = parse_ldap_group_query_string(
        env.str("MS_AUTH_LDAP_REQUIRE_GROUP", "")
    )
    AUTH_LDAP_USER_ATTR_MAP = {
        "email": env.str("MS_AUTH_LDAP_USER_ATTR_EMAIL"),
        "first_name": env.str("MS_AUTH_LDAP_USER_ATTR_FIRST_NAME"),
        "last_name": env.str("MS_AUTH_LDAP_USER_ATTR_LAST_NAME"),
    }

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            # Try loading custom corporate identity templates before falling back
            # to the defaults in the templates directory
            root("matshare", "ci_templates"),
            # The templates directory is added here explicitly because APP_DIRS won't
            # allow overriding templates of apps that come earlier in INSTALLED_APPS
            root("matshare", "templates"),
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "builtins": [
                # Load internationalization tags by default
                "django.templatetags.i18n",
                "django.templatetags.l10n",
                "django.templatetags.tz",
                # Some project-wide tags
                "matshare.templatetags.matshare_util",
            ],
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "matshare.context_processors.matshare_context_processor",
                "matshare.context_processors.easy_access_context_processor",
            ],
        },
    },
]

WSGI_APPLICATION = "matshare.wsgi.application"


# Database
# https://docs.djangoproject.com/en/3.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": env.str("MS_DATABASE_ENGINE", "django.db.backends.postgresql"),
        "HOST": env.str("MS_DATABASE_HOST", ""),
        "PORT": env.str("MS_DATABASE_PORT", ""),
        "NAME": env.str("MS_DATABASE_NAME", ""),
        "USER": env.str("MS_DATABASE_USER", ""),
        "PASSWORD": env.str("MS_DATABASE_PASSWORD", ""),
        "ATOMIC_REQUESTS": True,
        "CONN_MAX_AGE": env.int("MS_DATABASE_CONN_MAX_AGE", 0),
        "OPTIONS": env.json("MS_DATABASE_OPTIONS", {}),
    },
}


# Password validation
# https://docs.djangoproject.com/en/3.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Hash passwords using argon2 instead of the default pbkdf2
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]


# Internationalization
# https://docs.djangoproject.com/en/3.0/topics/i18n/

LANGUAGE_COOKIE_NAME = "ms_lang"

# We only offer the subset of all Django-provided languages MatShare was translated to
LANGUAGES = [
    (code, name) for code, name in AVAILABLE_DJANGO_LANGUAGES if code in ("en",)
]

# Fallback language in case the browser-requested one is unavailable
LANGUAGE_CODE = env.str("MS_LANGUAGE_CODE", "en")
_codes = [lang[0] for lang in LANGUAGES]
if LANGUAGE_CODE not in _codes:
    raise ImproperlyConfigured(f"MS_LANGUAGE_CODE must be one of: {_codes}")

TIME_ZONE = env.str("MS_TIME_ZONE", "UTC")

USE_I18N = True
USE_L10N = True
USE_TZ = True


# Absolute URL under which MatShare is accessible to the public
MS_URL = env.str("MS_URL", "https://my-domain").rstrip("/")
MS_URL_SPL = urllib.parse.urlsplit(MS_URL)

# URL to the root of the webserver running MatShare
MS_ROOT_URL = urllib.parse.urlunsplit(
    (MS_URL_SPL.scheme, MS_URL_SPL.netloc, "", MS_URL_SPL.query, MS_URL_SPL.fragment)
)

# uWSGI always mounts Matshare in /, but we want the URL reverser to return correct
# values
FORCE_SCRIPT_NAME = MS_URL_SPL.path


# User uploaded files
MEDIA_ROOT = os.path.abspath(env.str("MS_MEDIA_ROOT", root("media")))


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.0/howto/static-files/

STATIC_ROOT = os.path.abspath(env.str("MS_STATIC_ROOT", root("static")))
# Hosted by uWSGI in <MS_URL>/static/ by default
STATIC_URL = env.str("MS_STATIC_URL", MS_URL_SPL.path + "/static").rstrip("/") + "/"


# E-mail settings

DEFAULT_FROM_EMAIL = env.str("MS_DEFAULT_FROM_EMAIL", "MatShare <my-email@my-domain>")
# Use the same address as sender for error reports
SERVER_EMAIL = DEFAULT_FROM_EMAIL
MS_CONTACT_EMAIL = env.str("MS_CONTACT_EMAIL", "my-email@my-domain")
EMAIL_HOST = env.str("MS_EMAIL_HOST", "localhost")
EMAIL_HOST_USER = env.str("MS_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env.str("MS_EMAIL_HOST_PASSWORD", "")
EMAIL_PORT = env.int("MS_EMAIL_PORT", 25)
EMAIL_USE_SSL = env.bool("MS_EMAIL_USE_SSL", False)
EMAIL_USE_TLS = env.bool("MS_EMAIL_USE_TLS", False)
EMAIL_USE_LOCALTIME = True


# Logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "{asctime} {levelname} {name} {process:d} {thread:d} {message}",
            "style": "{",
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
    "root": {
        "handlers": ["console"],
        # Show INFO messages for all components in debug mode
        "level": "INFO" if DEBUG else "WARNING",
    },
    "loggers": {},
}
if DEBUG:
    for logger_name in env.tuple("MS_DEBUG_LOGGERS", default=()):
        LOGGING["loggers"][logger_name] = {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        }


# Password resetting
MS_PASSWORD_RESET = env.bool("MS_PASSWORD_RESET", True)
# How long should password reset links be valid
MS_PASSWORD_RESET_EXPIRATION_HOURS = env.int("MS_PASSWORD_RESET_EXPIRATION_HOURS", 2)
assert MS_PASSWORD_RESET_EXPIRATION_HOURS > 0


# Varios MatShare-specific settings

# Increment version whenever to prompt users for a consent again
CONSENTS = {
    # Whether the use of cookies was accepted
    "cookies": {"version": 1},
    # Whether the privacy policy was accepted upon login
    "privacy": {"version": 1},
}

# Default values for newly created courses
MS_COURSE_CONTRIBUTOR = env.str("MS_COURSE_CONTRIBUTOR", "")
MS_COURSE_PUBLISHER = env.str("MS_COURSE_PUBLISHER", "")

# Used for signatures of administrative commits, default is value of MS_CONTACT_EMAIL
MS_GIT_ADMIN_EMAIL = env.str("MS_GIT_ADMIN_EMAIL", MS_CONTACT_EMAIL)

# The branch (or tag) material and source tracking happens on
MS_GIT_MAIN_REF = env.str("MS_GIT_MAIN_REF", "refs/heads/master")

# Directory the git repositories of courses are stored in
MS_GIT_ROOT = os.path.abspath(env.str("MS_GIT_ROOT", root("git_repos")))

# Mapping of keys and values to add to git config when creating a repository
MS_GIT_EXTRA_CONFIG = env.dict("MS_GIT_EXTRA_CONFIG", default={})

# This will be set as core.hooksPath in git config when creating a repository
MS_GIT_HOOKS_DIR = os.path.abspath(env.str("MS_GIT_HOOKS_DIR", "git_hooks"))

# Contents of this directory are committed to newly created repositories
MS_GIT_INITIAL_DIR = os.path.abspath(env.str("MS_GIT_INITIAL_DIR", root("git_initial")))

# Subdirectories inside a course's git repository that hold edited material and sources
MS_GIT_EDIT_SUBDIR = env.str("MS_GIT_EDIT_SUBDIR", "edit")
MS_GIT_SRC_SUBDIR = env.str("MS_GIT_SRC_SUBDIR", "src")

# Matuc configuration file inside the edit subdirectory
MS_MATUC_CONFIG_FILE = ".lecture_meta_data.dcxml"
