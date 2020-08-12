"""
uWSGI signal handlers (timers for now).
"""

# This module is imported by the spooler process, which doesn't know anything about
# WSGI or Django, hence we need to set it up initially to be able to use the ORM
from django.conf import settings

if not settings.configured:
    import django

    django.setup()

import importlib
import logging

from django.db import transaction

# This will also set uwsgi.spooler to uwsgi_tasks's spooler callback during startup
import uwsgi_tasks

from .models import (
    Course,
    CourseEditorSubscription,
    CourseStudentSubscription,
    EasyAccess,
    MaterialBuild,
    NotificationFrequency,
    User,
)


LOGGER = logging.getLogger(__name__)


def _send_editor_notifications(notification_frequency):
    for user in (
        User.objects.filter(
            is_active=True,
            sources_notification_frequency=notification_frequency,
            editor_subscriptions__needs_notification=True,
        )
        .distinct()
        .iterator()
    ):
        LOGGER.debug("%r has pending notifications", user)
        with transaction.atomic():
            for sub in (
                CourseEditorSubscription.objects.filter(
                    user=user, needs_notification=True
                )
                .with_prefetching()
                .select_related("user")
                .select_for_update(of=("self",))
            ):
                LOGGER.info("Sending notification mail for %r", sub)
                sub.send_notification_mail()


def _send_student_notifications(notification_frequency):
    for user in (
        User.objects.filter(
            is_active=True, student_subscriptions__needs_notification=True
        )
        .distinct()
        .iterator()
    ):
        LOGGER.debug("%r has pending notifications", user)
        with transaction.atomic():
            for sub in (
                CourseStudentSubscription.objects.filter(
                    user=user,
                    notification_frequency=notification_frequency,
                    needs_notification=True,
                )
                .with_prefetching()
                .select_related("user")
                .select_for_update(of=("self",))
            ):
                LOGGER.info("Sending notification mail for %r", sub)
                sub.send_notification_mail()


@uwsgi_tasks.cron(minute=19)
def clear_material_builds(_):
    """Removes material builds of old revisions."""
    MaterialBuild.objects.clear_outdated()


@uwsgi_tasks.cron(hour=2, minute=29)
def clear_easy_access_tokens(_):
    """Removes expired EasyAccess tokens from database every night."""
    EasyAccess.objects.clear_expired()


@uwsgi_tasks.cron(minute=39)
def clear_sessions(_):
    """Removes expired sessions from database every hour."""
    engine = importlib.import_module(settings.SESSION_ENGINE)
    try:
        engine.SessionStore.clear_expired()
    except NotImplementedError:
        # Engine doesn't need clearing, no problem
        pass


# Notification mailing for the different notification frequencies

# Immediately means every 5 minutes
@uwsgi_tasks.cron(minute=-5)
def send_notifications_immediately(_):
    _send_editor_notifications(NotificationFrequency.immediately)
    _send_student_notifications(NotificationFrequency.immediately)


@uwsgi_tasks.cron(hour=-12, minute=5)
def send_notifications_twice_daily(_):
    _send_editor_notifications(NotificationFrequency.twice_daily)
    _send_student_notifications(NotificationFrequency.twice_daily)


@uwsgi_tasks.cron(hour=0, minute=15)
def send_notifications_daily(_):
    _send_editor_notifications(NotificationFrequency.daily)
    _send_student_notifications(NotificationFrequency.daily)


@uwsgi_tasks.cron(weekday=0, hour=0, minute=25)
def send_notifications_mon(_):
    _send_editor_notifications(NotificationFrequency.mon_fri)
    _send_student_notifications(NotificationFrequency.mon_fri)


@uwsgi_tasks.cron(weekday=4, hour=0, minute=25)
def send_notifications_fri(_):
    _send_editor_notifications(NotificationFrequency.mon_fri)
    _send_student_notifications(NotificationFrequency.mon_fri)


@uwsgi_tasks.cron(weekday=0, hour=0, minute=35)
def send_notifications_weekly(_):
    _send_editor_notifications(NotificationFrequency.weekly)
    _send_student_notifications(NotificationFrequency.weekly)
