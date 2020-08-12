import io
import os
import posixpath
import tempfile
import zipfile

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.validators import RegexValidator
from django.http import FileResponse, Http404, HttpResponseRedirect, QueryDict
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView, View
from django.views.generic.detail import SingleObjectMixin
import django_filters
import django_filters.views
import django_filters.widgets
from django_flexquery import Q
import pygit2
import watson.search

from ..git import utils as git_utils
from ..models import (
    Course,
    CourseEditorSubscription,
    CourseStudentSubscription,
    CourseType,
    MaterialBuild,
    StudyCourse,
    Term,
)
from ..utils import MatShareFilterSet, TypedMultipleValueField
from ..views import MatShareViewMixin


class SingleCourseViewMixin(SingleObjectMixin):
    """
    Fetches and stores the requested :class:`Course` object as ``self.object`` and
    implements access level checking, all of which forms the base for views dealing
    with a single course.
    """

    # Anonymous users are redirected to login page when access level isn't sufficient,
    # authenticated users get a PermissionDenied
    min_access_level = Course.AccessLevel.metadata
    # Whether Http404 should be raised when the course has static material
    no_static_courses = False

    def dispatch(
        self,
        request,
        study_course_slug,
        term_slug,
        type_slug,
        course_slug,
        *args,
        **kwargs,
    ):
        """Pull out slug kwargs and enforce ``self.min_access_level``."""
        self.object = get_object_or_404(
            self.get_queryset().by_slug_path(
                study_course_slug, term_slug, type_slug, course_slug
            )
        )
        self.access_level, self.easy_access = self.object.get_access_level(request)
        if self.access_level < self.min_access_level:
            if request.user.is_authenticated:
                raise PermissionDenied
            return redirect_to_login(request.build_absolute_uri())
        if self.object.is_static and self.no_static_courses:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        """Reuse ``self.object`` which has been fetched during :meth:`dispatch`."""
        return self.object

    def get_queryset(self):
        return (
            Course.objects.visible(self.request)
            .distinct()
            .with_prefetching()
            .with_access_level_prefetching()
        )


class CourseDetailViewBase(MatShareViewMixin, SingleCourseViewMixin, TemplateView):
    is_course_directory = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Make access level and easy access available
        ctx["access_level"] = self.access_level
        ctx["easy_access"] = self.easy_access
        # Whether user is allowed to change the course in the admin
        ctx["can_change_course"] = self.request.user.has_perm(
            self.object.get_perm("change"), self.object
        )
        return ctx

    def get_title_parts(self):
        return (str(self.object),)


class DirectoryView(MatShareViewMixin, django_filters.views.FilterView):
    """
    Allows searching for courses.
    """

    class DirectoryFilterSet(MatShareFilterSet):
        class Meta:
            model = Course
            fields = (
                "search",
                "study_course",
                "type",
                "term",
                "language",
                "editor",
                "subscribed",
            )

        search = django_filters.CharFilter(method="filter_search", label=_("Search"))
        study_course = django_filters.ModelChoiceFilter(
            queryset=StudyCourse.objects.all(),
            to_field_name="slug",
            empty_label=_("All courses of study"),
            label=_("course of study"),
        )
        type = django_filters.ModelChoiceFilter(
            queryset=CourseType.objects.all(),
            to_field_name="slug",
            empty_label=_("All types"),
        )
        term = django_filters.ModelChoiceFilter(
            queryset=Term.objects.all(),
            to_field_name="slug",
            empty_label=_("All terms"),
            null_label=_("Not related to a term"),
        )
        language = django_filters.ChoiceFilter(
            choices=Course.language.field.choices, empty_label=_("All languages")
        )
        subscribed = django_filters.BooleanFilter(
            method="filter_subscribed",
            widget=forms.CheckboxInput(),
            label=_("Only courses I'm subscribed to"),
        )
        editor = django_filters.BooleanFilter(
            method="filter_editor",
            widget=forms.CheckboxInput(),
            label=_("Only courses I'm editor of"),
        )

        def __init__(self, data=None, *args, **kwargs):
            current_term = Term.objects.get_current()
            # Filter for current term initially or with query parameter term=current
            if current_term is not None:
                if data is None:
                    data = QueryDict(mutable=True)
                    data["term"] = current_term.slug
                elif data.get("term") == "current":
                    data = data.copy()
                    data["term"] = current_term.slug
            super().__init__(*args, data=data, **kwargs)
            self.form.fields["term"].initial = current_term

        def filter_editor(self, queryset, name, value):
            if not value or not self.request.user.is_authenticated:
                return queryset
            return queryset.filter(editors=self.request.user)

        def filter_search(self, queryset, name, value):
            return watson.search.filter(queryset, value)

        def filter_subscribed(self, queryset, name, value):
            if not value or not self.request.user.is_authenticated:
                return queryset
            return queryset.filter(students=self.request.user)

    filterset_class = DirectoryFilterSet
    template_name = "matshare/course/directory.html"
    title = _("Course directory")
    is_course_directory = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Calculate access levels to be able to decide whether material is available
        for course in self.filterset.page:
            course.access_level, _ = course.get_access_level(self.request)
        return ctx

    def get_queryset(self):
        return (
            Course.objects.visible(self.request)
            .distinct()
            .with_prefetching()
            .with_access_level_prefetching()
            .order_by("name", "type__name", "-term__start_date")
        )


class GitView(CourseDetailViewBase):
    """
    Shows details on how to clone the git repository and lists user-specific ACLs.
    """

    template_name = "matshare/course/git.html"
    min_access_level = Course.AccessLevel.ro
    no_static_courses = True
    is_course_git = True

    def get_context_data(self, **kwargs):
        """Make git ACL available and reject EasyAccess users."""
        ctx = super().get_context_data(**kwargs)
        # Git is not available with EasyAccess
        if ctx["easy_access"] is not None:
            raise PermissionDenied
        ctx["git_acl"] = self.object.get_git_acl(self.request.user)
        return ctx

    def get_title_parts(self):
        return (_("Git access"), *super().get_title_parts())


class MaterialViewBase(CourseDetailViewBase):
    """
    Provides functionality for on-demand material building and status pages.
    """

    template_name = "matshare/course/material_build_status.html"
    min_access_level = Course.AccessLevel.material
    is_course_overview = True

    def build_status_page(self, builds):
        """
        Returns the response to show a build status page for given builds.
        """
        self.builds = builds
        return super().get(self.request, builds=builds)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["Status"] = MaterialBuild.Status
        ctx["builds"] = self.builds
        ctx["failed_builds"] = [
            build
            for build in self.builds
            if build.status == MaterialBuild.Status.failed
        ]
        return ctx

    def get_title_parts(self):
        return (_("Material is building"), *super().get_title_parts())


class MaterialDownloadView(MaterialViewBase):
    """
    Collects material of the course and all sub-courses and serves it for downloading.
    Multiple files are packed into a ZIP archive.
    """

    class DownloadForm(forms.Form):
        format = forms.ChoiceField(
            # Allow choosing by friendly name, not by the db integer value
            choices=zip(MaterialBuild.Format.names, MaterialBuild.Format.labels),
            required=False,
        )
        include_sub_courses = forms.BooleanField(required=False)

    def collect_builds_and_static_courses(self, format, include_sub_courses):
        """Return :class:`MaterialBuild` objects and courses with static material.

        Two tuples are returned.
        """
        builds = []
        static_courses = []
        if self.object.is_static:
            static_courses.append(self.object)
        elif self.object.material_revision:
            builds.append(
                MaterialBuild.objects.with_prefetching().get_or_create(
                    course=self.object,
                    format=format,
                    revision=self.object.material_revision,
                )[0]
            )
        if include_sub_courses:
            for course in self.object.sub_courses.all():
                if course.is_static:
                    static_courses.append(course)
                elif course.material_revision:
                    builds.append(
                        MaterialBuild.objects.with_prefetching().get_or_create(
                            course=course,
                            format=format,
                            revision=course.material_revision,
                        )[0]
                    )
        return tuple(builds), tuple(static_courses)

    def collect_material(self, builds, static_courses):
        """Return file-like object and filename of the collected material."""
        # Try if we could just offer a single file for downloading
        if len(builds) == 1 and not static_courses:
            root = builds[0].absolute_path
            if os.path.isdir(root):
                items = os.listdir(root)
                if len(items) == 1 and os.path.isfile(os.path.join(root, items[0])):
                    return (
                        open(os.path.join(root, items[0]), "rb"),
                        builds[0].course.download_name + os.path.splitext(items[0])[1],
                    )
        elif not builds and len(static_courses):
            root = static_courses[0].absolute_static_material_path
            if os.path.isdir(root):
                items = os.listdir(root)
                if len(items) == 1 and os.path.isfile(os.path.join(root, items[0])):
                    return (
                        open(os.path.join(root, items[0]), "rb"),
                        static_courses[0].download_name + os.path.splitext(items[0])[1],
                    )

        # We have to collect multiple files into a ZIP archive
        tmp = tempfile.NamedTemporaryFile(prefix="matshare_material_", suffix=".zip")
        # ZipFile won't close a file it hasn't opened itself, so tmp will survive
        with zipfile.ZipFile(tmp, mode="w", compression=zipfile.ZIP_DEFLATED) as zip:
            zip_root = self.object.download_name
            # Collect build results
            for build in builds:
                for root, dirnames, filenames in os.walk(build.absolute_path):
                    # Directory inside the ZIP file in which files are placed
                    zip_dir = posixpath.normpath(
                        posixpath.join(
                            zip_root,
                            build.course.download_name,
                            os.path.relpath(root, build.absolute_path),
                        )
                    )
                    for filename in filenames:
                        zip.write(
                            os.path.join(root, filename),
                            os.path.join(zip_dir, filename),
                        )
            # Collect static material
            for course in static_courses:
                for root, dirnames, filenames in os.walk(
                    course.absolute_static_material_path
                ):
                    # Directory inside the ZIP file in which files are placed
                    zip_dir = posixpath.normpath(
                        posixpath.join(
                            zip_root,
                            course.download_name,
                            os.path.relpath(root, course.absolute_static_material_path),
                        )
                    )
                    for filename in filenames:
                        zip.write(
                            os.path.join(root, filename),
                            os.path.join(zip_dir, filename),
                        )

        # Go back to the start of the file after ZipFile has written to it
        tmp.seek(0)

        # The temporary file will be deleted once .close() is called on it, which the
        # WSGI file wrapper MUST do due to WSGI spec after the file was fully sent off.
        return tmp, f"{self.object.download_name}.zip"

    def get(self, request):
        form = self.DownloadForm(request.GET)
        if not form.is_valid():
            raise Http404
        # Format isn't provided for a course with static material, so use some default
        if form.cleaned_data["format"]:
            format = MaterialBuild.Format[form.cleaned_data["format"]]
        else:
            format = next(iter(MaterialBuild.Format))
        include_sub_courses = form.cleaned_data.get("include_sub_courses", False)

        builds, static_courses = self.collect_builds_and_static_courses(
            format, include_sub_courses
        )
        if not builds and not static_courses:
            # Nothing to download
            raise Http404

        # Check if all builds have completed
        pending_builds = [
            build for build in builds if build.status != MaterialBuild.Status.completed
        ]
        if pending_builds:
            return self.build_status_page(pending_builds)

        # Ok, all builds completed
        # Update an eventual subscription of this course or those of super courses
        if request.user.is_authenticated:
            subs = (
                CourseStudentSubscription.objects.filter(
                    Q(course=self.object) | Q(course__sub_courses=self.object),
                    user=request.user,
                )
                .with_prefetching()
                .select_for_update(of=("self",))
            )
            for sub in subs:
                for build in builds:
                    # Not all builds are part of each subscription necessarily
                    if (
                        build.course == sub.course
                        or build.course in sub.course.sub_courses.all()
                    ):
                        sub.mark_downloaded(build.course)
                        # Don't send notification mails for revisions already downloaded
                        sub.mark_notified(build.course)
                sub.save()

        file, name = self.collect_material(builds, static_courses)
        return FileResponse(file, as_attachment=True, filename=name)


class MaterialHTMLView(MaterialViewBase):
    """
    Serves HTML material for online viewing.
    """

    no_static_courses = True

    def get(self, request, path="inhalt.html"):
        # There can be at most one build per course, format and revision
        build, _ = MaterialBuild.objects.with_prefetching().get_or_create(
            course=self.object,
            format=MaterialBuild.Format.html,
            revision=self.object.material_revision,
        )
        if build.status == MaterialBuild.Status.completed:
            # Great, serve the build result
            return self.serve_build_result(build, path)
        return self.build_status_page([build])

    def serve_build_result(self, build, rel_path):
        """Serve the requested file relative to the build results directory.

        Non-existent files and paths pointing outside the build results directory
        are answered by raising a :class:`Http404`.
        """
        build_root = os.path.realpath(build.absolute_path)
        full_path = os.path.realpath(os.path.join(build_root, rel_path))
        # Don't allow accessing files outside the build results directory
        if not full_path.startswith(build_root + os.sep):
            raise Http404
        try:
            return FileResponse(open(full_path, "rb"))
        except OSError:
            # File not found, permission denied etc.
            raise Http404


class OverviewView(CourseDetailViewBase):
    """
    Shows metadata, material links, sub/super-courses and subscription settings.
    """

    template_name = "matshare/course/overview.html"
    is_course_overview = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sub_courses"] = sorted(
            self.object.sub_courses.all(), key=lambda c: (c.name, c.type.name)
        )
        # Provide facilities for self-subscribing and updating subscription settings
        sub = None
        if (
            self.request.user.is_authenticated
            and self.access_level >= Course.AccessLevel.material
        ):
            try:
                sub = CourseStudentSubscription.objects.get(
                    course=self.object, user=self.request.user
                )
            except CourseStudentSubscription.DoesNotExist:
                pass
            ctx["student_subscription_form"] = StudentSubscriptionView.SubscriptionForm(
                initial={
                    "notification_frequency": self.request.user.default_material_notification_frequency
                    if sub is None
                    else sub.notification_frequency
                },
                instance=sub,
            )
        else:
            ctx["student_subscription_form"] = None
        ctx["student_subscription"] = sub
        return ctx

    def get_queryset(self):
        """Prefetch the sub-courses for listing them on the overview page."""
        return (
            super()
            .get_queryset()
            .prefetch_related("sub_courses", "sub_courses__term", "sub_courses__type")
        )


@method_decorator(never_cache, name="dispatch")
class SourcesView(CourseDetailViewBase):
    """
    Allows managing source files.
    """

    class DeleteForm(forms.Form):
        select = TypedMultipleValueField()

    class CreateDirectoryForm(forms.Form):
        name = forms.CharField(
            validators=(
                # This prevents using . or .. or creating invisible directories.
                RegexValidator(
                    r"^[^/\.][^/]*$",
                    message=_(
                        "Directory names may not start with a dot or contain slashes."
                    ),
                ),
            ),
            label=_("Name"),
        )

    class UploadForm(forms.Form):
        files = forms.FileField(
            widget=forms.ClearableFileInput(attrs={"multiple": True})
        )
        note = forms.CharField(
            max_length=2000,
            required=False,
            help_text=_(
                "If you think there's something special the person editing this material should be aware of, enter it here. Maximum 2000 characters."
            ),
            label=_("Add additional notes"),
            widget=forms.Textarea(),
        )

    template_name = "matshare/course/sources.html"
    min_access_level = Course.AccessLevel.ro
    no_static_courses = True
    is_course_sources = True

    def dispatch(self, request, path="", **kwargs):
        """Normalizes and splits the path and stores it as tuple in ``self.path``."""
        if not posixpath.isabs(path):
            path = f"/{path}"
        path = posixpath.normpath(path)
        self.path = tuple(part for part in path.split("/") if part)
        return super().dispatch(request, **kwargs)

    def find_node(self, path):
        """Search the commit under the repository's main reference for a path.

        As root for searching, ``MS_GIT_SRC_SUBDIR`` is assumed.
        The ``path`` must be an iterable of path components.
        It returns a :class:`pygit2.Tree` if the path was a directory or a
        :class:`pygit2.Blob` if it was a file.
        A :class:`KeyError` is raised if the path wasn't found.
        """
        commit = git_utils.resolve_committish(self.repo, settings.MS_GIT_MAIN_REF)
        node = commit.tree / settings.MS_GIT_SRC_SUBDIR
        for part in path:
            if not isinstance(node, pygit2.Tree):
                raise KeyError(part)
            node /= part
        # Commits are not supported and treated as if they didn't exist
        if isinstance(node, pygit2.Commit):
            raise KeyError(part)
        return node

    def get(self, request, delete_form=None, mkdir_form=None, upload_form=None):
        if self.access_level >= Course.AccessLevel.rw:
            if delete_form is None:
                delete_form = self.DeleteForm()
            if mkdir_form is None:
                mkdir_form = self.CreateDirectoryForm()
            if upload_form is None:
                upload_form = self.UploadForm()
        # Fetch the requested file or directory from git
        try:
            node = self.find_node(self.path)
        except KeyError:
            # List empty directory
            node = ()
        else:
            if isinstance(node, pygit2.Blob):
                # Serve the file contents in browser (Content-Disposition: inline)
                return FileResponse(io.BytesIO(node.read_raw()), filename=node.name)
        # Directory listing
        items = []
        for item in node:
            if isinstance(item, pygit2.Blob):
                items.append({"type": "file", "name": item.name, "size": item.size})
            elif isinstance(item, pygit2.Tree):
                items.append({"type": "dir", "name": item.name, "size": None})
        return super().get(
            request,
            items=tuple(items),
            delete_form=delete_form,
            mkdir_form=mkdir_form,
            upload_form=upload_form,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["path"] = "/".join(self.path)
        ctx["partial_paths"] = tuple(
            (part, posixpath.join(*self.path[: idx + 1]))
            for idx, part in enumerate(self.path)
        )
        return ctx

    def get_git_signature(self):
        """Return a :class:`pygit2.Signature` for the current user."""
        if self.easy_access is None:
            assert self.request.user.is_authenticated
            return git_utils.create_signature(
                self.request.user.get_full_name(), self.request.user.email
            )
        return git_utils.create_signature(self.easy_access.name, self.easy_access.email)

    def get_title_parts(self):
        return (_("Sources"), *super().get_title_parts())

    def handle_delete(self, delete_form):
        """Handle a valid :class:`DeleteForm` by removing files from git."""
        browser = git_utils.ContentBrowser(self.repo, settings.MS_GIT_MAIN_REF)
        rel_paths = []
        for name in delete_form.cleaned_data["select"]:
            name = posixpath.basename(name)
            if name in (".", ".."):
                # Ignore invalid file names
                continue
            rel_path = posixpath.join(*self.path, name)
            try:
                browser.remove(posixpath.join(settings.MS_GIT_SRC_SUBDIR, rel_path))
            except KeyError:
                # Ignore missing items
                continue
            rel_paths.append(rel_path)
        if rel_paths:
            rel_paths.sort()
            commit_msg = "Sources deleted\n\n" + "\n".join(rel_paths[:10])
            if len(rel_paths) > 10:
                commit_msg += f"\n... and {len(rel_paths)-10} more"
            commit_id = browser.commit(
                self.get_git_signature(), commit_msg, settings.MS_GIT_MAIN_REF
            )
            # Refresh because processing might have taken some time and locking
            # that long is no option
            self.object.refresh_from_db()
            self.object.mark_sources_updated(commit_id.hex)
            self.object.save()
            messages.success(
                self.request,
                _("{number} items have been deleted.").format(number=len(rel_paths)),
            )

    def handle_upload(self, upload_form):
        """Handle a valid :class:`UploadForm` by adding files to git."""
        browser = git_utils.ContentBrowser(self.repo, settings.MS_GIT_MAIN_REF)
        rel_paths = []
        for file in upload_form.files.getlist("files"):
            name = posixpath.basename(file.name)
            if name in (".", ".."):
                # Ignore invalid file names
                continue
            rel_path = posixpath.join(*self.path, name)
            rel_paths.append(rel_path)
            browser.add_from_bytes(
                posixpath.join(settings.MS_GIT_SRC_SUBDIR, rel_path), file.read()
            )
        if rel_paths:
            rel_paths.sort()
            commit_msg = "Sources added\n\n" + "\n".join(rel_paths[:10])
            if len(rel_paths) > 10:
                commit_msg += f"\n... and {len(rel_paths)-10} more"
            note = upload_form.cleaned_data.get("note")
            if note:
                commit_msg += "\n\n" + note
            commit_id = browser.commit(
                self.get_git_signature(), commit_msg, settings.MS_GIT_MAIN_REF
            )
            # Refresh because uploading might have taken some time and locking
            # that long is no option
            self.object.refresh_from_db()
            self.object.mark_sources_updated(commit_id.hex)
            self.object.save()
            messages.success(
                self.request,
                _("{number} files have been added.").format(number=len(rel_paths)),
            )

    def post(self, request):
        """Try handling all possible forms."""
        if self.access_level < Course.AccessLevel.rw:
            raise PermissionDenied
        mkdir_form = None
        if request.POST.get("mkdir"):
            mkdir_form = self.CreateDirectoryForm(request.POST)
            if mkdir_form.is_valid():
                # Don't add empty directory to git, just switch the view
                messages.info(
                    request,
                    _(
                        "The new directory will be created once you upload some "
                        "file into it."
                    ),
                )
                return self.redirect_to_path(
                    self.path + (mkdir_form.cleaned_data["name"],)
                )
        delete_form = None
        if request.POST.get("delete"):
            delete_form = self.DeleteForm(request.POST)
            if delete_form.is_valid():
                self.handle_delete(delete_form)
                return self.redirect_to_path(self.path)
        upload_form = None
        if request.POST.get("upload"):
            upload_form = self.UploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                self.handle_upload(upload_form)
                return self.redirect_to_path(self.path)
        return self.get(
            request,
            delete_form=delete_form,
            mkdir_form=mkdir_form,
            upload_form=upload_form,
        )

    def redirect_to_path(self, path):
        """Returns a response redirecting to given path inside the sources directory.

        The path must be given as tuple of path components.
        """
        if path:
            return HttpResponseRedirect(
                self.object.urls.reverse("course_sources", path="/".join(path))
            )
        return HttpResponseRedirect(self.object.urls.reverse("course_sources"))

    @cached_property
    def repo(self):
        """Open and cache the course's :class:`pygit2.Repository`."""
        return pygit2.Repository(self.object.absolute_repository_path)


class StudentSubscriptionView(LoginRequiredMixin, SingleCourseViewMixin, View):
    """
    Allows a user to subscribe/unsubscribe himself to/from a course.
    """

    class SubscriptionForm(forms.ModelForm):
        class Meta:
            model = CourseStudentSubscription
            fields = ("notification_frequency",)

    min_access_level = Course.AccessLevel.material

    def post(self, request):
        if request.POST.get("unsubscribe"):
            self.object.students.remove(request.user)
            messages.success(
                request,
                _("You have unsubscribed from {course}.").format(course=self.object),
            )
        else:
            try:
                sub = CourseStudentSubscription.objects.get(
                    course=self.object, user=request.user
                )
            except CourseStudentSubscription.DoesNotExist:
                sub = CourseStudentSubscription(
                    course=self.object,
                    user=request.user,
                    # Courses with static material generate no notification mails
                    needs_notification=not self.object.is_static,
                )
            form = self.SubscriptionForm(request.POST, instance=sub)
            if not form.is_valid():
                raise PermissionDenied
            form.save()
            messages.success(
                request,
                _("Your subscription to {course} was saved.").format(
                    course=self.object
                ),
            )
        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", self.object.get_absolute_url())
        )
