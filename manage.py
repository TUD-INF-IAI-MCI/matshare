#!/usr/bin/env python3
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "matshare.settings")
    # If nothing is configured, run management commands in debug mode to not require
    # MS_SECRET_KEY
    os.environ.setdefault("MS_DEBUG", "1")
    # If no database is configured, run with dummy values to allow basic management
    # commands
    if not os.getenv("MS_DATABASE_NAME"):
        os.environ.setdefault("MS_DATABASE_ENGINE", "django.db.backends.sqlite3")
        os.environ.setdefault("MS_DATABASE_NAME", "db.sqlite3")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
