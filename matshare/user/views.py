import hashlib

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.forms import PasswordChangeForm as _PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView, View
from feedgen.feed import FeedGenerator
import pygit2

from .. import __version__
from ..context_processors import matshare_context_processor
from ..git import utils as git_utils
from ..models import (
    Course,
    CourseEditorSubscription,
    CourseStudentSubscription,
    MaterialBuild,
    User,
)
from ..views import MatShareViewMixin


# The dashboard should always be reloaded to avoid outdated infos or missing updates
@method_decorator(never_cache, name="dispatch")
class DashboardView(LoginRequiredMixin, MatShareViewMixin, TemplateView):
    template_name = "matshare/user/dashboard.html"
    title = _("Dashboard")
    is_user_dashboard = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        is_editor = ctx["is_editor"] = CourseEditorSubscription.objects.filter(
            user=self.request.user
        ).exists()
        if is_editor:
            ctx["active_editor_subscriptions"] = list(
                CourseEditorSubscription.objects.filter(
                    user=self.request.user,
                    course__editing_status=Course.EditingStatus.in_progress,
                )
                .order_by("course__name", "course__type__name")
                .with_prefetching()
            )
        student_subscriptions = ctx["student_subscriptions"] = list(
            CourseStudentSubscription.objects.filter(
                user=self.request.user, active=True
            )
            .order_by("course__name", "course__type__name")
            .with_prefetching()
        )
        ctx["subscriptions_with_new_material"] = [
            sub for sub in student_subscriptions if sub.undownloaded_courses
        ]
        return ctx


class FeedViewBase(View):
    """
    Base for building user-specific atom feeds of subscribed courses.
    """

    def get(self, request, user_pk, feed_token):
        user = get_object_or_404(
            User, pk=user_pk, feed_token=feed_token, is_active=True
        )
        with user.localized():
            gen = FeedGenerator()
            gen.generator(
                generator="MatShare", version=__version__, uri=settings.MS_URL
            )
            gen.author(name="MatShare", email=settings.MS_CONTACT_EMAIL)
            gen.link(
                href=settings.MS_ROOT_URL + reverse("user_dashboard"), rel="alternate"
            )
            self.populate_feed(user, gen)
        return HttpResponse(gen.atom_str(), content_type="application/atom+xml")

    def populate_feed(self, user, gen):
        raise NotImplementedError


class EditorFeedView(FeedViewBase):
    """
    Feed that notifies when new sources were uploaded to a subscribed course.
    """

    def populate_feed(self, user, gen):
        gen.id(user.absolute_editor_feed_url)
        gen.title(
            _("Source updates for {full_name}").format(full_name=user.get_full_name())
        )
        for sub in (
            CourseEditorSubscription.objects.filter(
                user=user, course__editing_status=Course.EditingStatus.in_progress
            )
            # Only show courses to which sources were uploaded already
            .exclude(course__sources_revision="").with_prefetching()
        ):
            entry = gen.add_entry()
            entry.guid(
                hashlib.sha1(
                    f"{sub.course.name} {sub.course.sources_revision}".encode()
                ).hexdigest()
            )
            entry.updated(sub.course.sources_updated_last)
            entry.title(str(sub.course))
            entry.link(
                href=settings.MS_ROOT_URL + sub.course.get_absolute_url(),
                rel="alternate",
            )
            # Annotate course with the latest commits
            sub.course.latest_commits = []
            repo = pygit2.Repository(sub.course.absolute_repository_path)
            for parent, child in git_utils.walk_pairwise(
                repo, sub.course.sources_revision
            ):
                # Only respect commits that changed the src directory
                if next(
                    git_utils.paths_changed(parent, child, settings.MS_GIT_SRC_SUBDIR)
                ):
                    sub.course.latest_commits.append(
                        git_utils.extract_commit_info(child)
                    )
                    if len(sub.course.latest_commits) == 5:
                        break
            entry.content(
                render_to_string(
                    "matshare/user/editor_feed_entry_content.html",
                    {**matshare_context_processor(), "subscription": sub},
                ),
                type="CDATA",
            )


class StudentFeedView(FeedViewBase):
    """
    Feed that notifies when new material was uploaded to a subscribed course.
    """

    def populate_feed(self, user, gen):
        gen.id(user.absolute_student_feed_url)
        gen.title(
            _("Material updates for {full_name}").format(full_name=user.get_full_name())
        )
        student_subscriptions = CourseStudentSubscription.objects.filter(
            user=user, active=True
        ).with_prefetching()
        for sub in student_subscriptions:
            entry = gen.add_entry()
            # Build a GUID of material update times in the subscription. Course
            # names are included as well to notify about name changes.
            tokens = [
                f"{c.name} {c.material_updated_last.timestamp()}"
                for c in sub.all_courses
                if c.material_updated_last is not None
            ]
            # Make order deterministic to get a reproducible GUID
            tokens.sort()
            entry.guid(hashlib.sha1(" ".join(tokens).encode()).hexdigest())
            # Pick the most recent update time of all courses in the subscription
            entry.updated(
                max(
                    c.material_updated_last
                    for c in sub.all_courses
                    if c.material_updated_last is not None
                )
            )
            entry.title(str(sub.course))
            entry.link(
                href=settings.MS_ROOT_URL + sub.course.get_absolute_url(),
                rel="alternate",
            )
            courses = [
                c for c in sub.all_courses if c.material_updated_last is not None
            ]
            # Sort courses by last update time, recently updated ones first
            courses.sort(key=lambda c: c.material_updated_last, reverse=True)
            # Annotate courses with the latest commits
            for course in courses:
                # Ignore courses with no commits, i.e. those with static material
                if not course.material_revision:
                    continue
                course.latest_commits = []
                repo = pygit2.Repository(course.absolute_repository_path)
                for parent, child in git_utils.walk_pairwise(
                    repo, course.material_revision
                ):
                    # Only respect commits that changed the edit directory
                    if next(
                        git_utils.paths_changed(
                            parent, child, settings.MS_GIT_EDIT_SUBDIR
                        )
                    ):
                        course.latest_commits.append(
                            git_utils.extract_commit_info(child)
                        )
                        if len(course.latest_commits) == 5:
                            break
            entry.content(
                render_to_string(
                    "matshare/user/student_feed_entry_content.html",
                    {
                        **matshare_context_processor(),
                        "subscription": sub,
                        "courses": courses,
                    },
                ),
                type="CDATA",
            )


@method_decorator(never_cache, name="dispatch")
class SettingsView(LoginRequiredMixin, MatShareViewMixin, TemplateView):
    class PasswordChangeForm(_PasswordChangeForm):
        # Override the original field to disable autofocus
        old_password = forms.CharField(
            label=_("Old password"),
            strip=False,
            widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        )

        def save(self, commit=True):
            """Generate a new feed token after password was changed."""
            result = super().save(commit=commit)
            self.user.reset_feed_token()
            if commit:
                self.user.save()
            return result

    class SettingsForm(forms.ModelForm):
        class Meta:
            model = User
            fields = (
                "time_zone",
                "default_material_notification_frequency",
                "update_material_notification_frequencies",
                "sources_notification_frequency",
            )

        update_material_notification_frequencies = forms.BooleanField(
            required=False,
            label=_(
                "Also use this notification frequency for courses I've subscribed to already."
            ),
        )

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Only offer sources_notification_frequency field to editors
            if not CourseEditorSubscription.objects.filter(user=self.instance).exists():
                del self.fields["sources_notification_frequency"]

        def save(self, commit=True):
            """Updates notification frequency of existing subscriptions, if desired."""
            instance = super().save(commit=commit)
            if self.cleaned_data.get("update_material_notification_frequencies"):
                CourseStudentSubscription.objects.filter(user=instance).update(
                    notification_frequency=instance.default_material_notification_frequency
                )
            return instance

    template_name = "matshare/user/settings.html"
    title = _("My Settings")
    is_user_settings = True

    def get(self, request, password_form=None, settings_form=None):
        if password_form is None and request.user.has_usable_password():
            password_form = self.PasswordChangeForm(request.user)
        if settings_form is None:
            settings_form = self.SettingsForm(instance=request.user)
        return super().get(
            request, password_form=password_form, settings_form=settings_form
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["study_courses"] = sorted(
            self.request.user.study_courses.all(), key=lambda sc: sc.name
        )
        return ctx

    def post(self, request):
        password_form = None
        if request.POST.get("change_password"):
            if not request.user.has_usable_password():
                raise PermissionDenied
            password_form = self.PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                password_form.save()
                messages.success(
                    request,
                    _(
                        "Your password was changed. "
                        "Please log in again using the new password."
                    ),
                )
                messages.warning(
                    request,
                    _(
                        "The URL of your personal news feed has now changed for "
                        "security reasons as well. If you were using the feed, "
                        "add it to your feedreader again."
                    ),
                )
                return redirect("user_settings")
        settings_form = None
        if request.POST.get("change_settings"):
            settings_form = self.SettingsForm(request.POST, instance=request.user)
            if settings_form.is_valid():
                settings_form.save()
                messages.success(request, _("Your settings were saved."))
                return redirect("user_settings")
        return self.get(
            request, password_form=password_form, settings_form=settings_form
        )
