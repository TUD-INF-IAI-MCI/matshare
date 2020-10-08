import json
import logging
import os

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import get_script_prefix, reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
import pygit2

from . import utils as git_utils
from ..models import Course
from ..utils import basic_auth


LOGGER = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(basic_auth(realm="Git Access", max_header_size=200), name="dispatch")
class GitAuthView(View):
    """
    Authenticates requests to git and passes appropriate config to git-http-backend.
    """

    def dispatch(self, request, user, suffix="", **slug_path):
        course = get_object_or_404(
            Course.objects.by_slug_path(**slug_path)
            .visible(user)
            .distinct()
            .filter(is_static=False)
            .select_related("study_course")
            .prefetch_related("editors", "students")
        )
        acl = course.get_git_acl(user)
        # Instruct webserver to forward the request to git-http-backend
        response = HttpResponse()
        response["MS-Git-Auth"] = (
            user.username
            + ":"
            + json.dumps(
                {
                    "acl": acl,
                    # Internal URL to access uWSGI directly, without nginx
                    "push_notify_url": "http://"
                    + request.META["SERVER_NAME"]
                    + ":"
                    + request.META["SERVER_PORT"]
                    + "/"
                    + reverse("git_push_notify", kwargs={"course_pk": course.pk})[
                        len(get_script_prefix()) :
                    ].lstrip("/"),
                }
            )
        )
        return response


@method_decorator(csrf_exempt, name="dispatch")
class GitPushNotifyView(View):
    """
    Marks the course for being checked for rebuilding/notification sending.

    It expects a JSON POST request with a body of the format::

        {
            "user": <username>,
            "updates": [[<reference>, <old_revision>, <new_revision>], ...]
        }
    """

    def post(self, request, course_pk):
        if request.META.get("HTTP_X_FORWARDED_FOR"):
            # This view is not accessible externally through nginx, simple but effective
            raise PermissionDenied
        course = get_object_or_404(
            Course.objects.select_for_update(of=("self",)),
            pk=course_pk,
            is_static=False,
        )

        try:
            data = json.loads(request.body)
            assert isinstance(data, dict)
            username = data.get("user")
            assert isinstance(username, str)
            updates = data.get("updates")
            assert isinstance(updates, list)
            # Only update if the main reference has changed
            for item in updates:
                assert isinstance(item, list) and len(item) == 3
                ref, old_rev, new_rev = item
                assert isinstance(old_rev, str) and isinstance(new_rev, str)
                if ref == settings.MS_GIT_MAIN_REF:
                    break
            else:
                # Main reference hasn't changed, do nothing
                return HttpResponse()
            assert git_utils.OID_PATTERN.fullmatch(old_rev)
            assert git_utils.OID_PATTERN.fullmatch(new_rev)
        except (AssertionError, ValueError):
            # JSON decoding error or Invalid data structure, return 400 Bad Request
            return HttpResponse(status=400)

        if old_rev in git_utils.NULL_REFS or new_rev in git_utils.NULL_REFS:
            # Reference created or deleted, mark both as updated
            material_updated = True
            src_updated = True
        else:
            # Check what has changed between the two revisions
            repo = pygit2.Repository(course.absolute_repository_path)
            material_updated, src_updated = git_utils.paths_changed(
                repo.revparse_single(old_rev),
                repo.revparse_single(new_rev),
                settings.MS_GIT_EDIT_SUBDIR,
                settings.MS_GIT_SRC_SUBDIR,
            )

        if material_updated or src_updated:
            LOGGER.debug(
                "User %r updated %r for course %d from %r to %r (material=%r src=%r)",
                username,
                ref,
                course.pk,
                old_rev,
                new_rev,
                material_updated,
                src_updated,
            )
            if material_updated:
                course.mark_material_updated(new_rev)
            if src_updated:
                course.mark_sources_updated(new_rev)
            course.save()
        return HttpResponse(content_type="text/plain")
