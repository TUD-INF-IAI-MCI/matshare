import contextlib
import datetime
import functools
import logging
import os
import posixpath
from xml.dom import minidom
import xml.etree.ElementTree as ET

from django.conf import settings
from django.conf.global_settings import LANGUAGES
from django.contrib.auth.models import AbstractUser, UserManager as _UserManager
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateRangeField,
    JSONField,
    RangeBoundary,
    RangeOperators,
)
from django.core import validators as django_validators
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.db import models
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_flexquery import FlexQuery, Manager, Q, QuerySet
from django_flexquery.contrib.user_based import UserBasedFlexQuery
import pygit2
import pytz
import rules
from rules.contrib.models import RulesModel, RulesModelBase
from timezone_field import TimeZoneField
import watson.search

from .git import utils as git_utils
from .utils import ISBNField, IntegerEnumField, MatShareEmailMessage


LOGGER = logging.getLogger(__name__)


@rules.predicate
def _is_editor(user, obj):
    """Whether user is editor of a specific course or any course (``obj=None``)."""
    if not user.is_authenticated:
        return False
    if obj is None:
        return user.edited_courses.all().exists()
    return user in obj.editors.all()


@rules.predicate
def _course_has_editing_status_in_progress(user, obj):
    return obj is None or obj.editing_status == Course.EditingStatus.in_progress


# Give staff and editors access to the admin interface of MatShare's models, each
# model then declares its own, fine-grained permissions
rules.add_perm("matshare", rules.is_staff | _is_editor)


@rules.predicate
def _obj_is_regular_user(user, obj):
    """Predicate used to ensure staff members can't alter staff and superusers."""
    return obj is None or not obj.is_staff and not obj.is_superuser


def create_slug_mixin(
    derive_from="name",
    field_name="slug",
    max_length=20,
    blank=False,
    db_index=True,
    help_text=None,
    unique=True,
    validators=(),
    verbose_name=_("slug"),
    **field_kwargs,
):
    if help_text is None:
        if derive_from is None:
            help_text = _(
                "A short representation of alphanumerics and dashes for use in URLs. "
                "No more than {max_length} characters long."
            ).format(max_length=max_length)
        else:
            help_text = _(
                "A short representation of alphanumerics and dashes for use in URLs. "
                "No more than {max_length} characters long. "
                "Leave blank to get an auto-generated one."
            ).format(max_length=max_length)
    # Plug together an abstract model (w/o db table) with _SlugModelMixin mixed in
    return type(_SlugModelMixin)(
        _SlugModelMixin.__name__,
        (_SlugModelMixin, models.Model),
        {
            "Meta": type("Meta", (), {"abstract": True}),
            # Django's metaclass machinery needs the module...
            "__module__": _SlugModelMixin.__module__,
            field_name: models.CharField(
                max_length=max_length,
                # Form validation would fail with blank=False and thus not allow
                # auto-deriving of slugs, hence we reject blank slugs manually in
                # clean_fields()
                blank=True,
                db_index=db_index,
                help_text=help_text,
                unique=unique,
                validators=(*validators, django_validators.validate_slug),
                verbose_name=verbose_name,
                **field_kwargs,
            ),
            "_slug_derive_from": derive_from,
            "_slug_field": field_name,
            "_slug_blank": blank,
        },
    )


class _SlugModelMixin:
    def clean_fields(self, exclude=None):
        """Auto-derive slug from another field or coerce existing ones to lowercase."""
        if not exclude or self._slug_field not in exclude:
            slug = getattr(self, self._slug_field)
            if not slug and self._slug_derive_from is not None:
                slug = getattr(self, self._slug_derive_from)
            # Ensures lowercase among other things
            slug = slugify(slug)
            if not self._slug_blank and not slug:
                raise ValidationError({self._slug_field: [_("A slug is required.")]})
            setattr(self, self._slug_field, slug)
        super().clean_fields(exclude=exclude)


class DateRange(models.Func):
    """
    Adapter for using the PostgreSQL DATERANGE function in Django's ORM.
    """

    function = "DATERANGE"
    output_field = DateRangeField()


class NotificationFrequency(models.IntegerChoices):
    """
    How often to send notificatzion mails about changes at most.
    """

    immediately = 100, _("Immediately after something has been uploaded")
    twice_daily = 200, _("Twice a day")
    daily = 300, _("Once a day")
    mon_fri = 400, _("Every Monday and Friday")
    weekly = 500, _("Once a week")
    never = 600, _("Never send notification mails")


class Model(RulesModel):
    """
    Custom model base class for all MatShare models.
    """

    class Meta:
        abstract = True

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.pk}: {self}>"


class CourseQuerySet(QuerySet):
    @FlexQuery.from_func
    def by_slug_path(base, study_course_slug, term_slug, type_slug, course_slug):
        """Filter for a course by the slugs of its related objects.

        When a course with no term should be looked up, the value of ``term_slug``
        has to be ``Course.NO_TERM_SLUG``.

        The resulting query set will yield either a single result or none.
        """
        # We're using sub-queries rather than JOINs because they'll yield at most
        # a single result each and then the index over study_course, term, type and
        # slug can be utilized
        return Q(
            study_course__in=StudyCourse.objects.filter(slug=study_course_slug),
            type__in=CourseType.objects.filter(slug=type_slug),
            slug=course_slug,
        ) & (
            Q(term=None)
            if term_slug == Course.NO_TERM_SLUG
            else Q(term__in=Term.objects.filter(slug=term_slug))
        )

    def get_by_natural_key(self, study_course, term, type, slug):
        return self.get(study_course=study_course, term=term, type=type, slug=slug)

    @FlexQuery.from_func
    def visible(base, request_or_user):
        """Filters for courses that might be accessed by this request and/or user.

        It filters for courses for which :meth:`Course.get_access_level` would return
        something else than ``Course.AccessLevel.none``, but all work is done solely
        by the database.

        Note that the resulting query set may contain the same course multiple times
        due to JOINs, so call ``distinct()`` on it if desired.
        """
        if isinstance(request_or_user, HttpRequest):
            request = request_or_user
            user = request.user
        else:
            request = None
            user = request_or_user

        # Staff sees everything
        if user.is_staff:
            return Q()

        # EasyAccess only works when a request was provided
        if request is None:
            explicit_pks = ()
        else:
            # Make courses authorized for via EasyAccess visible
            explicit_pks = [int(pk) for pk in request.session.get("easy_access", ())]
        q_easy_access = Q(pk__in=explicit_pks)

        # Anonymous users also see public courses
        if not user.is_authenticated:
            return q_easy_access | Q(metadata_audience=Course.Audience.public)

        # Now cover all variants of metadata_audience for authenticated users
        return q_easy_access | (
            # Public courses and those visible to all authenticated users
            Q(metadata_audience__lte=Course.Audience.users)
            # Courses visible to study courses the user is in
            | Q(metadata_audience=Course.Audience.study_course)
            & Q(study_course__students__in=(user,))
            # Courses the user is editor of
            | Q(editors__in=(user,))
            # Courses the user is subscribed to
            | Q(students__in=(user,))
        )

    def with_access_level_prefetching(self):
        """Prefetch all fields required for calculating access levels."""
        return self.select_related("study_course").prefetch_related(
            "editors",
            "student_subscriptions",
            "student_subscriptions__user",
            "super_courses",
            "super_courses__student_subscriptions",
            "super_courses__student_subscriptions__user",
        )

    def with_prefetching(self):
        """Prefetch fields needed for displaying."""
        return self.select_related("term", "type")


def _get_default_course_publisher():
    return settings.MS_COURSE_PUBLISHER


class Course(create_slug_mixin(max_length=150, unique=False), Model):
    """
    A course, the main building block of MatShare.
    """

    class AccessLevel(models.IntegerChoices):
        """
        Different access levels for a course's contents.
        """

        none = 100, _("None")
        metadata = 200, _("View metadata")
        material = 300, _("View material")
        ro = 400, _("View material and sources")
        rw = 500, _("View and modify material and sources")

        def __or__(self, other):
            """Binary-or-ing two access levels returns the higher one."""
            if not isinstance(other, type(self)):
                return NotImplemented
            return self if self > other else other

    class Audience(models.IntegerChoices):
        public = 100, _("Everyone")
        users = 200, _("Authenticated users")
        study_course = 300, _("Students of the course of study")
        subscribers = 400, _("Students subscribed to this course")

    class EditingStatus(models.IntegerChoices):
        in_progress = 100, _("In progress")
        complete = 200, _("Complete")
        suspended = 300, _("Suspended")
        cancelled = 400, _("Cancelled")

    class URLReverser:
        """
        Allows for easy reversing of course-related URLs, including all slugs
        automatically.
        """

        def __init__(self, course):
            self._course = course

        def reverse(self, view_name, **extra_kwargs):
            """Build a URL with all fields needed to identify the course."""
            return reverse(
                view_name,
                kwargs={
                    "study_course_slug": self._course.study_course.slug,
                    "term_slug": self._course.NO_TERM_SLUG
                    if self._course.term is None
                    else self._course.term.slug,
                    "type_slug": self._course.type.slug,
                    "course_slug": self._course.slug,
                    **extra_kwargs,
                },
            )

        def __getitem__(self, name):
            """Allows access from templates."""
            return self.reverse(name)

    class WatsonSearchAdapter(watson.search.SearchAdapter):
        """
        Implements weighted indexing of courses for searching with watson.
        """

        def get_title(self, obj):
            return obj.name

        def get_description(self, obj):
            return ""

        def get_content(self, obj):
            """Return all fields to be indexed by the search engine joined together."""
            tokens = (obj.doi, obj.isbn, obj.isbn_dashed, obj.author, obj.publisher)
            return " ".join(token for token in tokens if token)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("study_course", "term", "type", "slug"),
                name="%(app_label)s_%(class)s_unique_with_term",
            ),
            # This second constraint is needed because the first one doesn't catch
            # on multiple objects with term=None
            models.UniqueConstraint(
                fields=("study_course", "type", "slug"),
                condition=Q(term=None),
                name="%(app_label)s_%(class)s_unique_without_term",
            ),
            # Ensure revisions are unset for courses with static material
            models.CheckConstraint(
                check=Q(is_static=False)
                | Q(material_revision="") & Q(sources_revision=""),
                name="%(app_label)s_%(class)s_is_static_coherency",
            ),
        )
        verbose_name = _("course")
        verbose_name_plural = _("courses")
        rules_permissions = {
            "add": rules.is_staff,
            # Editors are allowed to change some settings while editing is in progress
            "change": rules.is_staff
            | _is_editor & _course_has_editing_status_in_progress,
            "delete": rules.is_staff,
            "view": rules.is_staff | _is_editor,
        }

    # Value of the URL slug field representing term=None
    NO_TERM_SLUG = "-"

    objects = CourseQuerySet.as_manager()

    name = models.CharField(
        max_length=150,
        help_text=_(
            "Required. A human-readable name to identify this course. "
            "Maximum 150 characters long."
        ),
        verbose_name=_("name"),
    )
    study_course = models.ForeignKey(
        "StudyCourse",
        on_delete=models.CASCADE,
        related_name="courses",
        verbose_name=_("course of study"),
    )
    term = models.ForeignKey(
        "Term",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_(
            "The term the course is held in, if it's related to a term at all."
        ),
        verbose_name=_("term"),
    )
    type = models.ForeignKey(
        "CourseType", on_delete=models.CASCADE, verbose_name=_("type")
    )
    creator = models.ForeignKey(
        "User", null=True, on_delete=models.SET_NULL, verbose_name=_("creator")
    )
    date_created = models.DateTimeField(auto_now_add=True, verbose_name=_("created"))
    editing_status = IntegerEnumField(
        EditingStatus,
        default=EditingStatus.in_progress,
        verbose_name=_("editing status"),
    )
    internal_reference = models.CharField(
        max_length=150,
        blank=True,
        help_text=_(
            "Optional. An internal reference, such as an ID of the course in another system."
        ),
        verbose_name=_("internal reference"),
    )

    # Permissions
    metadata_audience = IntegerEnumField(
        Audience,
        default=Audience.public,
        help_text=_(
            "Who can see this course (and its metadata) in the course directory."
        ),
        verbose_name=_("visibility"),
    )
    material_audience = IntegerEnumField(
        Audience,
        default=Audience.subscribers,
        help_text=_("Who is able to view and download material."),
        verbose_name=_("access to material"),
    )

    # Metadata
    doi = models.CharField(
        max_length=1000,
        blank=True,
        validators=(
            django_validators.RegexValidator(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$"),
        ),
        help_text=_('Digital Object Identifier, starts with "10.".'),
        verbose_name="DOI",
    )
    isbn = ISBNField(blank=True, help_text=_("International Standard Book Number"))
    author = models.CharField(max_length=300, blank=True, verbose_name=_("author"))
    # Matuc only supports these languages
    language = models.CharField(
        max_length=2,
        default="en",
        choices=[
            (code, name) for code, name in LANGUAGES if code in ("de", "en", "fr")
        ],
        help_text=_("The primary language of the material."),
        verbose_name=_("language"),
    )
    publisher = models.CharField(
        max_length=300,
        blank=True,
        default=_get_default_course_publisher,
        verbose_name=_("publisher"),
    )
    source_format = models.CharField(
        max_length=150,
        blank=True,
        help_text=_(
            "The format of sources provided by the author, such as PDF or PowerPoint."
        ),
        verbose_name=_("source format"),
    )

    # Courses that have static imported material have this flag set and won't get
    # a repository
    is_static = models.BooleanField(default=False)

    # Matuc settings
    magsbs_appendix_prefix = models.BooleanField(
        default=False, verbose_name=_("use appendix prefix")
    )
    magsbs_generate_toc = models.BooleanField(
        default=True, verbose_name=_("generate table of contents")
    )
    magsbs_page_numbering_gap = models.PositiveSmallIntegerField(
        default=5,
        validators=(django_validators.MinValueValidator(1),),
        verbose_name=_("page numbering gap"),
    )
    magsbs_toc_depth = models.PositiveSmallIntegerField(
        default=5,
        validators=(django_validators.MinValueValidator(1),),
        verbose_name=_("depth of table of contents"),
    )

    # Other courses that are part of this one
    sub_courses = models.ManyToManyField(
        "course",
        blank=True,
        related_name="super_courses",
        through="SubCourseRelation",
        # The first is the field referencing to this side of the relation
        through_fields=("super_course", "sub_course"),
        verbose_name=_("sub courses"),
    )

    # Related users
    editors = models.ManyToManyField(
        "User",
        through="CourseEditorSubscription",
        related_name="edited_courses",
        verbose_name=_("editors"),
    )
    students = models.ManyToManyField(
        "User",
        through="CourseStudentSubscription",
        related_name="subscribed_courses",
        verbose_name=_("students"),
    )

    material_revision = models.CharField(
        max_length=64, blank=True, verbose_name=_("latest revision of edit directory")
    )
    material_updated_last = models.DateTimeField(
        null=True, blank=True, verbose_name=_("material updated last")
    )
    sources_revision = models.CharField(
        max_length=64,
        blank=True,
        verbose_name=_("latest revision of sources directory"),
    )
    sources_updated_last = models.DateTimeField(
        null=True, blank=True, verbose_name=_("sources updated last")
    )

    def __str__(self):
        if self.term is None:
            return f"{self.type} {self.name}"
        return f"{self.type} {self.name} ({self.term})"

    def _ensure_not_is_static(self):
        """Raise ``RuntimeError`` when course has static material and no repository."""
        if self.is_static:
            raise RuntimeError(f"{self!r} has static material and hence no repository")

    def _ensure_is_static(self):
        """Raise ``RuntimeError`` when course has no static material."""
        if not self.is_static:
            raise RuntimeError(f"{self!r} has no static material")

    @cached_property
    def absolute_git_clone_url(self):
        """The absolute URL for cloning the course's git repository."""
        self._ensure_not_is_static()
        return settings.MS_ROOT_URL + self.urls.reverse("git_auth")

    @cached_property
    def absolute_repository_path(self):
        """The absolute path of this course's git repository.

        This is ``self.repository_path``, prepended with the directory configured
        by the ``MS_GIT_ROOT`` setting.
        """
        self._ensure_not_is_static()
        return os.path.join(settings.MS_GIT_ROOT, self.repository_path)

    @cached_property
    def absolute_static_material_path(self):
        """Absolute path of the directory this course's static material resides in."""
        self._ensure_is_static()
        return os.path.join(
            settings.MEDIA_ROOT,
            "static_courses",
            self.study_course.slug,
            self.NO_TERM_SLUG if self.term is None else self.term.slug,
            self.type.slug,
            self.slug,
            "material",
        )

    def clean(self):
        """Ensure ``metadata_audience`` isn't more restricted than ``material_audience``."""
        super().clean()
        if (
            self.metadata_audience is not None
            and self.material_audience is not None
            and self.metadata_audience > self.material_audience
        ):
            raise ValidationError(
                _(
                    "The access to material must be at least as restricted as "
                    "the overall visibility for these two concepts to make sense."
                )
            )

    @cached_property
    def download_name(self):
        """File/directory name to use when offering material for downloading."""
        return str(self).replace("/", "-").replace("\\", "-")

    def generate_matuc_config(self):
        """Generate the content of matuc's XML config file for this course.

        UTF-8 encoded ``bytes`` is returned.
        """
        self._ensure_not_is_static()
        root = ET.Element("metadata")
        root.attrib["xmlns:dc"] = "http://purl.org/dc/elements/1.1"
        root.attrib["xmlns:MAGSBS"] = "http://elvis.inf.tu-dresden.de"

        # Dublincore elements
        ET.SubElement(root, "dc:contributor").text = settings.MS_COURSE_CONTRIBUTOR
        ET.SubElement(root, "dc:creator").text = self.author
        ET.SubElement(root, "dc:date").text = str(self.date_created.year)
        ET.SubElement(root, "dc:language").text = self.language
        ET.SubElement(root, "dc:publisher").text = self.publisher
        ET.SubElement(root, "dc:rights").text = {
            self.Audience.public: "public access",
            self.Audience.users: "access for members only",
            self.Audience.study_course: "access for members of the course of study only",
            self.Audience.subscribers: "access for subscribed students only",
        }[self.material_audience]
        ET.SubElement(root, "dc:source").text = self.source_format
        ET.SubElement(root, "dc:title").text = self.name

        # Matuc settings
        ET.SubElement(root, "MAGSBS:appendixPrefix").text = str(
            int(self.magsbs_appendix_prefix)
        )
        ET.SubElement(root, "MAGSBS:generateToc").text = str(
            int(self.magsbs_generate_toc)
        )
        ET.SubElement(root, "MAGSBS:pageNumberingGap").text = str(
            self.magsbs_page_numbering_gap
        )
        ET.SubElement(root, "MAGSBS:sourceAuthor").text = self.author
        ET.SubElement(root, "MAGSBS:tocDepth").text = str(self.magsbs_toc_depth)

        # This is the minimum matuc version supporting all used config keys;
        # bump it when new backwards-incompatible keys are added
        ET.SubElement(root, "MAGSBS:version").text = "0.8"

        # Build UTF-8 bytes with pretty indentation
        return minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(
            indent="  ", encoding="utf-8"
        )

    def get_absolute_url(self):
        """URL of the course's detail page."""
        return self.urls.reverse("course_overview")

    def get_access_level(self, request_or_user):
        """Computes the :class:`Course.AccessLevel` for given request and//or user.

        All models of authorization that could lead to an access level are considered:

        *  staff members and editors always have write access
        *  EasyAccess (if a :class:django.http.HttpRequest` is given)
        *  the ``access_level`` field of a :class:`CourseStudentSubscription`
        *  the course's ``material_audience`` and ``metadata_audience`` fields

        Two values are returned: The `class:`AccessLevel` object and the
        :class:`EasyAccess` object that led to this access level. If the
        authorization is not EasyAccess-based, the second value will be ``None``.
        """
        # The code flow in this function looks a bit fiddly, that's because it hardly
        # tries to do only as much as really needed. Since this method is called in
        # most requests, optimizing here makes a lot of sense and should justify the
        # somewhat longer code. Comments are there to help understanding what's it
        # all about.
        if isinstance(request_or_user, HttpRequest):
            request = request_or_user
            user = request.user
        else:
            request = None
            user = request_or_user

        # Staff always has write access
        if user.is_staff:
            return self.AccessLevel.rw, None

        # Authorization via EasyAccess link
        easy_access = None
        level = self.AccessLevel.none
        if request is not None:
            try:
                easy_access_pk = request.session["easy_access"][str(self.pk)]
                easy_access = (
                    EasyAccess.objects.valid()
                    # Double-check course matches, just for consistency
                    .get(pk=easy_access_pk, course=self)
                )
                level = easy_access.access_level
            except KeyError:
                # No token for this course stored in the session
                pass
            except EasyAccess.DoesNotExist:
                # Token no longer exists, clean up session data accordingly
                del request.session["easy_access"][str(self.pk)]
            else:
                if level == self.AccessLevel.rw:
                    # It can't get any higher, stop here
                    return level, easy_access

        if user.is_authenticated:
            # Editors always have write access
            if user in self.editors.all():
                return self.AccessLevel.rw, None
            # Subscribed students have their own access level
            _level = level
            # Subscribed students inherit their access from super courses,
            # so we take the maximum level
            for sub in self.student_subscriptions.all():
                if sub.user == user and sub.access_level > _level:
                    _level = sub.access_level
            for course in self.super_courses.all():
                for sub in course.student_subscriptions.all():
                    if sub.user == user and sub.access_level > _level:
                        _level = sub.access_level
            # EasyAccess with same access_level takes precedence over subscription
            if _level > level:
                level = _level
                easy_access = None

        # The remaining checks won't yield a level higher than material, so maybe
        # we can skip them straight away
        if level >= self.AccessLevel.material:
            return level, easy_access

        # Check if self.material_audience applies
        if (
            self.material_audience == self.Audience.public
            or user.is_authenticated
            and (
                self.material_audience == self.Audience.users
                or self.material_audience == self.Audience.study_course
                and self.study_course in user.study_courses.all()
            )
        ):
            return self.AccessLevel.material, None

        # Last resort is self.metadata_audience
        if level == self.AccessLevel.metadata:
            return level, easy_access
        if (
            self.metadata_audience == self.Audience.public
            or user.is_authenticated
            and (
                self.metadata_audience == self.Audience.users
                or self.metadata_audience == self.Audience.study_course
                and self.study_course in users.study_courses.all()
            )
        ):
            return self.AccessLevel.metadata, None

        # All available authorization methods exhausted, no access
        return self.AccessLevel.none, None

    def get_git_acl(self, user):
        """Return a tuple of git ACL entries for given user.

        If the user's access level is lower than ``Course.AccessLevel.ro``, a
        :class:`django.core.exceptions.PermissionDenied` is raised.
        """
        self._ensure_not_is_static()
        access_level, _ = self.get_access_level(user)
        if access_level < self.AccessLevel.ro:
            raise PermissionDenied
        if user.is_staff:
            # Staff members have full access to every reference
            return (("*", "*", 1),)
        if access_level == self.AccessLevel.ro:
            # No write access
            return ()
        assert access_level == self.AccessLevel.rw
        return (
            (
                settings.MS_GIT_MAIN_REF,
                posixpath.join(
                    settings.MS_GIT_EDIT_SUBDIR, settings.MS_MATUC_CONFIG_FILE
                ),
                0,
            ),
            # Grant write access to edit and sources directories
            (
                settings.MS_GIT_MAIN_REF,
                posixpath.join(settings.MS_GIT_EDIT_SUBDIR, "*"),
                1,
            ),
            (
                settings.MS_GIT_MAIN_REF,
                posixpath.join(settings.MS_GIT_SRC_SUBDIR, "*"),
                1,
            ),
        )

    def mark_material_updated(self, new_rev):
        """Update git revision in which material was last updated.

        All student subscriptions of this course and super-courses are marked for
        notification mail sending.
        """
        self._ensure_not_is_static()
        self.material_revision = "" if new_rev in git_utils.NULL_REFS else new_rev
        self.material_updated_last = timezone.now()
        CourseStudentSubscription.objects.filter(
            Q(course=self) | Q(course__sub_courses=self)
        ).update(needs_notification=True)

    def mark_sources_updated(self, new_rev):
        """Update git revision in which sources where last updated.

        All student subscriptions of this course are marked for notification mail
        sending.
        """
        self._ensure_not_is_static()
        self.sources_revision = "" if new_rev in git_utils.NULL_REFS else new_rev
        self.sources_updated_last = timezone.now()
        CourseEditorSubscription.objects.filter(course=self).update(
            needs_notification=True
        )

    def natural_key(self):
        return self.study_course, self.term, self.type, self.slug

    @cached_property
    def repository_path(self):
        """Relative path of this course's git repository.

        This path is relative to the directory configured as ``MS_GIT_ROOT``.
        """
        self._ensure_not_is_static()
        return os.path.join(
            self.study_course.slug,
            self.NO_TERM_SLUG if self.term is None else self.term.slug,
            self.type.slug,
            self.slug,
        )

    @cached_property
    def urls(self):
        """A cached instance of :class:`Course.URLReverser`."""
        return self.URLReverser(self)

    def validate_unique(self, exclude=None):
        """Validate unique constraints before saving actually to get sensible errors."""
        if (
            self.term is None
            and (
                not exclude
                or "study_course" not in exclude
                and "term" not in exclude
                and "type" not in exclude
                and "slug" not in exclude
            )
            and type(self)
            .objects.exclude(pk=self.pk)
            .filter(
                study_course=self.study_course,
                term=None,
                type=self.type,
                slug=self.slug,
            )
            .exists()
        ):
            raise ValidationError(
                _(
                    "A course with this combination of course of study, term, type "
                    "and slug already exists."
                )
            )
        super().validate_unique(exclude=exclude)


class SubCourseRelation(Model):
    """
    Relation describing a :class:`Course` is part of another :class:`Course`.
    """

    class Meta:
        constraints = (
            models.CheckConstraint(
                check=~Q(sub_course=models.F("super_course")),
                name="%(app_label)s_%(class)s_no_self_relation",
            ),
        )
        unique_together = (("super_course", "sub_course"),)
        rules_permissions = {
            "add": rules.is_staff,
            "change": rules.is_staff,
            "delete": rules.is_staff,
            "view": rules.is_staff,
        }

    super_course = models.ForeignKey(
        "Course",
        on_delete=models.CASCADE,
        related_name="relations_to_sub_courses",
        verbose_name=_("super-course"),
    )
    sub_course = models.ForeignKey(
        "Course",
        on_delete=models.CASCADE,
        related_name="relations_to_super_courses",
        verbose_name=_("sub-course"),
    )

    def __str__(self):
        return f"{self.sub_course} << {self.super_course}"

    def clean(self):
        """Forbid making a course a sub-course of itself."""
        super().clean()
        if self.super_course == self.sub_course:
            raise ValidationError(_("A course can't be a sub-course of itself."))


class CourseEditorSubscriptionQuerySet(QuerySet):
    def get_by_natural_key(self, course, user):
        return self.get(course=course, user=user)

    def with_prefetching(self):
        """Prefetch commonly used fields."""
        return self.select_related("course", "course__term", "course__type", "user")


class CourseEditorSubscription(Model):
    """
    Relation describing a :class:`User` is responsible for editing a :class:`Course`.
    """

    class Meta:
        unique_together = (("course", "user"),)
        rules_permissions = {
            "add": rules.is_staff,
            "change": rules.is_staff,
            "delete": rules.is_staff,
            "view": rules.is_staff,
        }

    objects = CourseEditorSubscriptionQuerySet.as_manager()

    course = models.ForeignKey(
        "Course",
        on_delete=models.CASCADE,
        related_name="editor_subscriptions",
        verbose_name=_("course"),
    )
    user = models.ForeignKey(
        "User",
        on_delete=models.CASCADE,
        related_name="editor_subscriptions",
        verbose_name=_("user"),
    )
    # Revision for which a notification mail was sent last
    last_notified_revision = models.CharField(max_length=40, null=True)
    # Whether the subscription should be checked for notification mail sending
    needs_notification = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user} @ {self.course}"

    def send_notification_mail(self):
        """Send mail about new sources to the user."""
        if (
            self.course.sources_revision is None
            or self.last_notified_revision == self.course.sources_revision
        ):
            return

        # Annotate course with the commits since the last notification
        self.course.latest_commits = []
        repo = pygit2.Repository(self.course.absolute_repository_path)
        for parent, child in git_utils.walk_pairwise(
            repo, self.course.sources_revision, self.last_notified_revision
        ):
            # Only respect commits that changed the src directory
            if next(git_utils.paths_changed(parent, child, settings.MS_GIT_SRC_SUBDIR)):
                self.course.latest_commits.append(git_utils.extract_commit_info(child))
                if len(self.course.latest_commits) == 10:
                    break

        # Send the mail
        with self.user.localized():
            try:
                MatShareEmailMessage(
                    (self.user.email,),
                    _("New sources for {course}").format(course=self.course),
                    "matshare/email/sources_notification.html",
                    template_context={"subscription": self, "user": self.user},
                ).send()
            except OSError as err:
                LOGGER.error("Failed to send notification mail for %r: %r", self, err)
                # Don't mark as notified to have the mail re-sent next time
                return

        # Mark as notified
        self.last_notified_revision = self.course.sources_revision
        self.needs_notification = False
        self.save()


class CourseStudentSubscriptionQuerySet(QuerySet):
    def get_by_natural_key(self, course, user):
        return self.get(course=course, user=user)

    def with_prefetching(self):
        """Prefetch fields for displaying courses and finding unseen material."""
        return self.select_related(
            "course", "course__term", "course__type", "user"
        ).prefetch_related("course__sub_courses")


class CourseStudentSubscription(Model):
    """
    Relation describing a :class:`User` attends a :class:`Course`.
    """

    class Meta:
        index_together = (
            # Used for finding subscriptions for notification sending
            ("user", "active"),
        )
        unique_together = (("course", "user"),)
        rules_permissions = {
            "add": rules.is_staff,
            "change": rules.is_staff,
            "delete": rules.is_staff,
            "view": rules.is_staff,
        }

    objects = CourseStudentSubscriptionQuerySet.as_manager()

    course = models.ForeignKey(
        "Course",
        on_delete=models.CASCADE,
        related_name="student_subscriptions",
        verbose_name=_("course"),
    )
    user = models.ForeignKey(
        "User",
        on_delete=models.CASCADE,
        related_name="student_subscriptions",
        verbose_name=_("user"),
    )
    access_level = IntegerEnumField(
        Course.AccessLevel,
        exclude=(Course.AccessLevel.none, Course.AccessLevel.metadata),
        default=Course.AccessLevel.material,
        verbose_name=_("access level"),
    )
    active = models.BooleanField(
        default=True,
        help_text=_(
            "When you've finished a course, make its subscription inactive. "
            "Inactive subscriptions don't trigger material notification mails and "
            "the course won't appear in dashboard and material feed. However, you "
            "will retain your permission to access the course and its material "
            "via the course directory."
        ),
        verbose_name=_("active"),
    )
    notification_frequency = IntegerEnumField(
        NotificationFrequency,
        help_text=_(
            "How often notification mails about new material should be sent at most."
        ),
        verbose_name=_("e-mail notification frequency"),
    )
    # Stores which revisions of course and sub-courses were downloaded last
    last_downloaded_revisions = JSONField(default=dict)
    # For which revisions of course and sub-courses notification mails have been sent
    last_notified_revisions = JSONField(default=dict)
    # Whether the subscription should be checked for notification mail sending
    needs_notification = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user} @ {self.course}"

    @cached_property
    def all_courses(self):
        """Tuple of course and all sub-courses included in the subscription."""
        return (self.course, *self.course.sub_courses.all())

    def clean(self):
        """Set notification frequency based on user's default."""
        if self.notification_frequency is None:
            self.notification_frequency = (
                self.user.default_material_notification_frequency
            )
        super().clean()

    def mark_downloaded(self, course):
        """Mark current revision of given course as downloaded."""
        self.last_downloaded_revisions[str(course.pk)] = course.material_revision
        # Invalidate cache
        self.__dict__.pop("undownloaded_courses", None)

    def mark_notified(self, course):
        """Store that a notification mail has been sent for given course."""
        self.last_notified_revisions[str(course.pk)] = course.material_revision
        # Invalidate cache
        self.__dict__.pop("unnotified_courses", None)

    def natural_key(self):
        return self.course, self.user

    def send_notification_mail(self):
        """Send mail about new material to the user."""
        # Annotate courses with the commits since the last notification
        for course in self.unnotified_courses:
            course.latest_commits = []
            repo = pygit2.Repository(course.absolute_repository_path)
            for parent, child in git_utils.walk_pairwise(
                repo,
                course.material_revision,
                self.last_notified_revisions.get(str(course.pk)),
            ):
                # Only respect commits that changed the edit directory
                if next(
                    git_utils.paths_changed(parent, child, settings.MS_GIT_EDIT_SUBDIR)
                ):
                    course.latest_commits.append(git_utils.extract_commit_info(child))
                    if len(course.latest_commits) == 10:
                        break

        # Send the mail
        if self.unnotified_courses:
            with self.user.localized():
                try:
                    MatShareEmailMessage(
                        (self.user.email,),
                        _("New material for {course}").format(course=self.course),
                        "matshare/email/material_notification.html",
                        template_context={
                            "subscription": self,
                            "formats": MaterialBuild.Format,
                            "user": self.user,
                        },
                    ).send()
                except OSError as err:
                    LOGGER.error(
                        "Failed to send notification mail for %r: %r", self, err
                    )
                    # Don't mark as notified to have the mail re-sent next time
                    return

        # Mark as notified
        for course in self.unnotified_courses:
            self.mark_notified(course)
        self.needs_notification = False
        self.save()

    @cached_property
    def undownloaded_courses(self):
        """All courses (incl. sub-courses) with material not downloaded yet."""
        return tuple(
            sorted(
                (
                    course
                    for course in self.all_courses
                    if course.material_revision
                    and self.last_downloaded_revisions.get(str(course.pk))
                    != course.material_revision
                ),
                key=lambda course: (course.name, course.type.name),
            )
        )

    @cached_property
    def unnotified_courses(self):
        """All courses (incl. sub-courses) with pending notification mail."""
        return tuple(
            sorted(
                (
                    course
                    for course in self.all_courses
                    if course.material_revision
                    and self.last_notified_revisions.get(str(course.pk))
                    != course.material_revision
                ),
                key=lambda course: (course.name, course.type.name),
            )
        )


class EasyAccessQuerySet(QuerySet):
    def clear_expired(self):
        """Deletes expired tokens."""
        num, _ = self.exclude(self.valid.as_q()).delete()
        LOGGER.debug("Deleted %d expired easy access tokens", num)

    @FlexQuery.from_func
    def valid(base):
        return Q(expiration_date__gte=timezone.now().date())


def _get_default_easy_access_token_expiration_date():
    """Access tokens expire in 365 days by default."""
    return timezone.now().date() + datetime.timedelta(days=365)


class EasyAccess(Model):
    """
    URL granting accountless access to a single course.
    """

    class Meta:
        verbose_name = _("EasyAccess token")
        verbose_name_plural = _("EasyAccess tokens")
        rules_permissions = {
            "add": rules.is_staff,
            "change": rules.is_staff,
            "delete": rules.is_staff,
            "view": rules.is_staff,
        }

    objects = EasyAccessQuerySet.as_manager()

    token = models.CharField(
        max_length=20,
        default=functools.partial(get_random_string, 20),
        unique=True,
        verbose_name=_("token"),
    )
    course = models.ForeignKey(
        "Course",
        on_delete=models.CASCADE,
        related_name="easy_accesses",
        verbose_name=_("course"),
    )
    access_level = IntegerEnumField(
        Course.AccessLevel,
        exclude=(Course.AccessLevel.none,),
        default=Course.AccessLevel.material,
        help_text=_("The level of access granted when using this EasyAccess link."),
        verbose_name=_("access level"),
    )
    expiration_date = models.DateField(
        default=_get_default_easy_access_token_expiration_date,
        help_text=_("The URL won't be usable after this date."),
        verbose_name=_("expiration date"),
    )
    name = models.CharField(
        max_length=150,
        help_text=_("Name of the person this token is for, i.e. for commit messages."),
        verbose_name=_("name"),
    )
    email = models.EmailField(
        help_text=_("E-mail address of the person who will be using this link."),
        verbose_name=_("e-mail address"),
    )

    def __str__(self):
        return f"{self.name} <{self.email}> @ {self.course}"

    @cached_property
    def absolute_activation_url(self):
        """URL for using the token."""
        return settings.MS_ROOT_URL + reverse(
            "easy_access_activation", kwargs={"token": self.token}
        )

    # Use the default language and time zone
    @method_decorator(timezone.override(settings.TIME_ZONE))
    @method_decorator(translation.override(settings.LANGUAGE_CODE))
    def send_email(self):
        """Sends a notification e-mail with URL to the associated address."""
        try:
            MatShareEmailMessage(
                (self.email,),
                _("Authorization for {course}").format(course=self.course),
                "matshare/email/easy_access.html",
                template_context={"easy_access": self, "name": self.name},
            ).send()
        except OSError as err:
            LOGGER.error("Failed to send EasyAccess mail for %r: %r", self, err)


class CourseTypeManager(Manager):
    def get_by_natural_key(self, slug):
        return self.get(slug=slug.lower())


class CourseType(create_slug_mixin(max_length=20), Model):
    """
    Type of a course.
    """

    class Meta:
        verbose_name = _("course type")
        verbose_name_plural = _("course types")
        rules_permissions = {
            "add": rules.is_superuser,
            "change": rules.is_superuser,
            "delete": rules.is_superuser,
            "view": rules.is_staff,
        }

    objects = CourseTypeManager()

    name = models.CharField(max_length=150, verbose_name=_("name"))

    def __str__(self):
        return self.name

    def natural_key(self):
        return (self.slug,)


class MaterialBuildQuerySet(QuerySet):
    def clear_outdated(self):
        """Remove all builds except those of the current revisions."""
        num, _ = self.exclude(revision=models.F("course__material_revision")).delete()
        LOGGER.debug("Deleted %d outdated material builds", num)

    def with_prefetching(self):
        """Prefetch fields used in many methods of :class:`MaterialBuild`."""
        return self.select_related(
            "course", "course__term", "course__type"
        ).prefetch_related("course__sub_courses")


class MaterialBuild(Model):
    """
    A single build process for given course, format and revision.
    """

    class Format(models.IntegerChoices):
        epub = 100, "EPUB"
        html = 200, "HTML"

    class Status(models.IntegerChoices):
        waiting = 100, _("waiting")
        building = 200, _("building")
        completed = 300, _("completed")
        failed = 400, _("failed")

    class Meta:
        unique_together = (("course", "format", "revision"),)
        verbose_name = _("material build")
        verbose_name_plural = _("material builds")
        rules_permissions = {
            "add": rules.always_false,
            "change": rules.always_false,
            "delete": rules.is_staff,
            "view": rules.is_staff,
        }

    objects = MaterialBuildQuerySet.as_manager()

    course = models.ForeignKey(
        "Course",
        on_delete=models.CASCADE,
        related_name="material_builds",
        verbose_name=_("course"),
    )
    revision = models.CharField(max_length=40, verbose_name=_("revision"))
    format = IntegerEnumField(Format, verbose_name=_("format"))
    status = IntegerEnumField(Status, default=Status.waiting, verbose_name=_("status"))
    error_message = models.TextField(blank=True, verbose_name=_("error message"))
    date_created = models.DateTimeField(auto_now_add=True, verbose_name=_("created"))
    date_done = models.DateTimeField(null=True, verbose_name=_("done"))

    def __repr__(self):
        return (
            f"<MaterialBuild {self.pk}: "
            f"course={self.course!r} revision={self.revision[:7]} "
            f"format={self.format.name} status={self.status.name}>"
        )

    def __str__(self):
        return f"{self.course} ({self.revision[:7]}, {self.format.label})"

    @cached_property
    def absolute_path(self):
        """Absolute path of the build results directory."""
        return os.path.join(
            settings.MEDIA_ROOT,
            "material_builds",
            self.course.study_course.slug,
            self.course.NO_TERM_SLUG
            if self.course.term is None
            else self.course.term.slug,
            self.course.type.slug,
            self.course.slug,
            self.revision,
            self.format.name,
        )


class StudyCourseManager(Manager):
    def get_by_natural_key(self, slug):
        return self.get(slug=slug.lower())


class StudyCourse(create_slug_mixin(max_length=50), Model):
    """
    A course of study.
    """

    class Meta:
        verbose_name = _("course of study")
        verbose_name_plural = _("courses of study")
        rules_permissions = {
            "add": rules.is_superuser,
            "change": rules.is_superuser,
            "delete": rules.is_superuser,
            "view": rules.is_staff,
        }

    objects = StudyCourseManager()

    name = models.CharField(max_length=150, verbose_name=_("name"))

    def __str__(self):
        return self.name

    def natural_key(self):
        return (self.slug,)


class TermManager(Manager):
    def get_by_natural_key(self, slug):
        return self.get(slug=slug.lower())

    def get_current(self):
        """Get the currently active term or ``None``, if outside of all terms."""
        today = timezone.now().date()
        try:
            return self.get(start_date__lte=today, end_date__gte=today)
        except Term.DoesNotExist:
            return None


class Term(
    create_slug_mixin(
        max_length=20, validators=(django_validators.MinLengthValidator(3),)
    ),
    Model,
):
    """
    A teaching period.
    """

    class Meta:
        constraints = (
            ExclusionConstraint(
                expressions=(
                    (
                        DateRange(
                            "start_date",
                            "end_date",
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                name="%(app_label)s_%(class)s_no_overlapping",
            ),
        )
        ordering = ("-start_date",)
        verbose_name = _("term")
        verbose_name_plural = _("terms")
        rules_permissions = {
            "add": rules.is_superuser,
            "change": rules.is_superuser,
            "delete": rules.is_superuser,
            "view": rules.is_staff,
        }

    objects = TermManager()

    name = models.CharField(max_length=150, blank=False, verbose_name=_("name"))
    start_date = models.DateField(verbose_name=_("start date"))
    end_date = models.DateField(verbose_name=_("end date"))

    def __str__(self):
        return self.name

    def clean(self):
        """Ensure start date <= end date and terms don't overlap."""
        super().clean()
        if self.start_date is None or self.end_date is None:
            return
        if self.start_date > self.end_date:
            raise ValidationError(
                _("A term's start date can't be later than its end date.")
            )
        other_terms = type(self).objects.all()
        if self.pk is not None:
            other_terms = other_terms.exclude(pk=self.pk)
        try:
            other_term = other_terms.get(
                Q(start_date__lte=self.start_date) & Q(end_date__gte=self.start_date)
                | Q(start_date__lte=self.end_date) & Q(end_date__gte=self.end_date)
            )
        except self.DoesNotExist:
            pass
        else:
            raise ValidationError(
                _(
                    "The term overlaps with {other_term}, which lasts from "
                    "{other_term.start_date} to {other_term.end_date}."
                ).format(other_term=other_term)
            )

    def natural_key(self):
        """Reference terms by slug when serializing."""
        return (self.slug,)


class UserManager(_UserManager):
    def get_by_natural_key(self, username):
        return self.get(username=username.lower())


def _get_default_user_time_zone():
    return pytz.timezone(settings.TIME_ZONE)


class User(Model, AbstractUser, metaclass=RulesModelBase):
    """
    Custom user model, based on Django's default user schema.
    """

    class Meta:
        rules_permissions = {
            "add": rules.is_staff,
            "change": rules.is_superuser | rules.is_staff & _obj_is_regular_user,
            "delete": rules.is_superuser | rules.is_staff & _obj_is_regular_user,
            "view": rules.is_staff,
        }

    # Tells the createsuperuser management command which fields are mandatory
    REQUIRED_FIELDS = ("email", "first_name", "last_name")

    objects = UserManager()

    # Change max length of usernames
    username = models.CharField(
        max_length=50,
        unique=True,
        help_text=_(
            "Required. An unique, unchangeable identifier for the user account, "
            "50 characters or fewer."
        ),
        verbose_name=AbstractUser.username.field.verbose_name,
    )
    # Make email mandatory
    email = models.EmailField(verbose_name=AbstractUser.email.field.verbose_name)
    # Make first and last name mandatory
    first_name = models.CharField(
        max_length=50, verbose_name=AbstractUser.first_name.field.verbose_name
    )
    last_name = models.CharField(
        max_length=50, verbose_name=AbstractUser.last_name.field.verbose_name
    )

    study_courses = models.ManyToManyField(
        "StudyCourse",
        blank=True,
        related_name="students",
        help_text=_("The courses of study this user is matriculated to, if any."),
        verbose_name=_("courses of study"),
    )

    # Personal settings
    language = models.CharField(
        max_length=10,
        choices=settings.LANGUAGES,
        # Is populated upon first login, until which settings.LANGUAGE_CODE is assumed
        null=True,
        blank=True,
        verbose_name=_("preferred language"),
    )
    time_zone = TimeZoneField(
        default=_get_default_user_time_zone,
        help_text=_(
            "All dates on the website and in e-mails sent to you will be displayed in this time zone."
        ),
        verbose_name=_("preferred time zone"),
    )
    default_material_notification_frequency = IntegerEnumField(
        NotificationFrequency,
        default=NotificationFrequency.daily,
        help_text=_(
            "How often to send notification mails about new material at most. "
            "This only affects courses you subscribe to in the future."
        ),
        verbose_name=_("default e-mail notification frequency for new material"),
    )
    sources_notification_frequency = IntegerEnumField(
        NotificationFrequency,
        default=NotificationFrequency.immediately,
        help_text=_(
            "How often to send notification mails about new sources to edit at most."
        ),
        verbose_name=_("e-mail notification frequency for new sources"),
    )

    # Random token for accessing the user's atom feeds
    feed_token = models.CharField(
        max_length=20, default=functools.partial(get_random_string, 20)
    )

    # Password reset related fields
    password_reset_token = models.CharField(max_length=20, blank=True)
    password_reset_expiration_date = models.DateTimeField(
        # Use a safe default instead of null=True to avoid the checks for None all over
        default=datetime.datetime.fromtimestamp(0, timezone.utc)
    )

    def __str__(self):
        return f"{self.get_full_name()} ({self.username})"

    @cached_property
    def absolute_editor_feed_url(self):
        """Absolute URL to the user's personal sources feed."""
        return settings.MS_ROOT_URL + reverse(
            "user_editor_feed",
            kwargs={"user_pk": self.pk, "feed_token": self.feed_token},
        )

    @cached_property
    def absolute_student_feed_url(self):
        """Absolute URL to the user's personal material feed."""
        return settings.MS_ROOT_URL + reverse(
            "user_student_feed",
            kwargs={"user_pk": self.pk, "feed_token": self.feed_token},
        )

    def clean(self):
        """Rewrite username and email to lowercase."""
        super().clean()
        self.username = self.username.lower()
        self.email = self.email.lower()

    @contextlib.contextmanager
    def localized(self):
        """Context manager that activates user-specific locale and time zone."""
        with translation.override(self.language or settings.LANGUAGE_CODE):
            with timezone.override(self.time_zone):
                yield

    def reset_feed_token(self):
        """Generates a new random value for ``self.feed_token``."""
        self.feed_token = type(self).feed_token.field.get_default()
