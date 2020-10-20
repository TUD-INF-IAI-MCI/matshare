import os
import shutil
import tempfile
import zipfile

from django import forms
from django.contrib import admin
from django.contrib.admin.options import IS_POPUP_VAR
from django.contrib.admin.widgets import (
    ForeignKeyRawIdWidget,
    url_params_from_lookup_dict,
)
from django.contrib.auth.admin import UserAdmin as _UserAdmin
from django.contrib.auth.forms import (
    AdminPasswordChangeForm as _AdminPasswordChangeForm,
    UserCreationForm as _UserCreationForm,
)
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from rules.contrib import admin as rules_admin
from watson.admin import SearchAdmin

from .models import (
    Course,
    CourseEditorSubscription,
    CourseStudentSubscription,
    CourseType,
    EasyAccess,
    MaterialBuild,
    StudyCourse,
    SubCourseRelation,
    Term,
    User,
)
from .course.spooled_tasks import (
    spooled_import_course_repository,
    spooled_update_matuc_config,
)


class AdminSite(admin.AdminSite):
    # Change CI of admin pages
    index_title = _("Welcome to the MatShare Administration Tool")
    site_header = _("MatShare Administration")
    site_title = _("MatShare Admin")

    def has_permission(self, request):
        # This permission is registered in matshare.models
        return request.user.has_perm("matshare")

    def login(self, request, extra_context=None):
        """Use the regular login view even for the admin site."""
        return redirect_to_login(request.GET.get("next", ""))


admin_site = AdminSite()


@admin.register(Course, site=admin_site)
class CourseAdmin(SearchAdmin, rules_admin.ObjectPermissionsModelAdmin):
    class ChangeCourseForm(forms.ModelForm):
        static_material_upload = forms.FileField(
            required=False,
            help_text=_(
                "Upload a ZIP file containing the material to offer for download."
            ),
            label=_("upload material"),
            widget=forms.FileInput(attrs={"accept": ".zip"}),
        )

        def clean_static_material_upload(self):
            file = self.cleaned_data["static_material_upload"]
            if file is None:
                return None
            # Activate is_static when material was uploaded when creating a course
            if self.instance.pk is None:
                self.instance.is_static = True
            # Store uploaded data in temporary file. That file will get removed when
            # this form is garbage-collected
            tmp = tempfile.NamedTemporaryFile()
            for chunk in file.chunks():
                tmp.write(chunk)
            tmp.seek(0)
            try:
                with zipfile.ZipFile(tmp) as zip:
                    zip.testzip()
            except zipfile.BadZipFile:
                # Don't wait for the garbage collection
                tmp.close()
                raise ValidationError(_("The uploaded file is no valid ZIP file."))
            tmp.seek(0)
            return tmp

        def save(self, commit=True):
            file = self.cleaned_data["static_material_upload"]
            if file is not None:
                self.instance.material_updated_last = timezone.now()
            obj = super().save(commit=commit)
            if file is not None:
                # Unpack static material
                with tempfile.TemporaryDirectory() as tmpdir:
                    with zipfile.ZipFile(file) as zip:
                        zip.extractall(tmpdir)
                    if os.path.exists(obj.absolute_static_material_path):
                        shutil.rmtree(obj.absolute_static_material_path)
                    shutil.copytree(
                        tmpdir,
                        obj.absolute_static_material_path,
                        # Don't use copy2 to not preserve file mode, because we
                        # don't trust the people uploading material do them right
                        copy_function=shutil.copyfile,
                    )
                    # Fix permissions that were preserved from temporary directory
                    os.chmod(obj.absolute_static_material_path, 0o755)
                # Don't wait for the garbage collection
                file.close()
            return obj

    class CloneCourseForm(forms.ModelForm):
        class Meta:
            model = Course
            fields = (
                "name",
                "slug",
                "term",
                "internal_reference",
                "editing_status",
                "metadata_audience",
                "material_audience",
            )

    class CourseEditorSubscriptionInline(rules_admin.ObjectPermissionsTabularInline):
        model = CourseEditorSubscription
        fields = ("user",)
        raw_id_fields = ("user",)
        ordering = ("user__username",)
        extra = 0
        classes = ("collapse",)
        verbose_name = _("editor")
        verbose_name_plural = _("editors")

    class CourseStudentSubscriptionInline(rules_admin.ObjectPermissionsTabularInline):
        model = CourseStudentSubscription
        fields = ("user", "access_level")
        raw_id_fields = ("user",)
        ordering = ("user__username",)
        extra = 0
        classes = ("collapse",)
        verbose_name = _("subscribed student")
        verbose_name_plural = _("subscribed students")

    class EasyAccessInline(rules_admin.ObjectPermissionsTabularInline):
        class EasyAccessForm(forms.ModelForm):
            send_email = forms.BooleanField(
                required=False,
                help_text=_(
                    "Send an e-mail with the EasyAccess URL to the associated address."
                ),
                label=_("Send e-mail?"),
            )
            url = forms.CharField(
                required=False,
                widget=forms.TextInput(attrs={"readonly": "readonly"}),
                help_text=_("URL to use the token and gain access to this course."),
                label=_("URL"),
            )

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if self.instance.pk is None:
                    # Default to sending an email when creating a new token
                    self.fields["send_email"].initial = True
                else:
                    # Display the absolute token activation URL read-only in the formset
                    self.fields["url"].initial = self.instance.absolute_activation_url

            def save(self, commit=True):
                # Send e-mail to token owner
                obj = super().save(commit=commit)
                if self.cleaned_data.get("send_email"):
                    obj.send_email()
                return obj

        model = EasyAccess
        fields = (
            "name",
            "email",
            "access_level",
            "expiration_date",
            "send_email",
            "url",
        )
        form = EasyAccessForm
        ordering = ("name", "access_level", "expiration_date")
        extra = 0
        classes = ("collapse",)

        def get_queryset(self, request):
            """Only list EasyAccess URL that haven't expired yet."""
            return super().get_queryset(request).valid()

    class SubCourseInline(rules_admin.ObjectPermissionsTabularInline):
        class SubCourseWidget(ForeignKeyRawIdWidget):
            """
            Custom raw id widget that filters the courses shown for only those with
            no own sub courses to prevent nested sub-courses.
            """

            def url_parameters(self):
                params = super().url_parameters()
                params.update(
                    url_params_from_lookup_dict({"sub_courses__isnull": True})
                )
                return params

        model = SubCourseRelation
        # Foreign key pointing to the course this inline is shown on
        fk_name = "super_course"
        fields = ("sub_course",)
        raw_id_fields = ("sub_course",)
        ordering = (
            "sub_course__name",
            "sub_course__type",
        )
        extra = 0
        # Link the change view for every sub-course for quick editing
        show_change_link = True
        classes = ("collapse",)
        verbose_name = _("sub-course")
        verbose_name_plural = _("sub-courses")

        def formfield_for_foreignkey(self, db_field, request, **kwargs):
            """Swap out the widget of the sub course field for the custom one."""
            field = super().formfield_for_foreignkey(db_field, request, **kwargs)
            if db_field.name == "sub_course":
                field.widget = self.SubCourseWidget(
                    field.widget.rel,
                    field.widget.admin_site,
                    attrs=field.widget.attrs,
                    using=field.widget.db,
                )
            return field

        def get_max_num(self, request, obj=None, **kwargs):
            """Only allow creating sub-course relations if no super-courses exist."""
            if obj is not None and obj.super_courses.all().exists():
                return 0
            return super().get_max_num(request, obj=obj, **kwargs)

    class Media:
        css = {"all": ("admin/css/inline_hide_object_str.css",)}

    form = ChangeCourseForm
    list_display = ("name", "type", "term", "study_course")
    list_filter = (
        "study_course",
        "term",
        "type",
        "editing_status",
        "metadata_audience",
        "material_audience",
    )
    ordering = ("-term__start_date", "name", "type__name")
    search_fields = (
        "name",
        "slug",
        "study_course__name",
        "internal_reference",
    )
    change_form_template = "admin/course/change_form.html"

    # These fields will be blindly copied when cloning a course. For relations,
    # only forward foreign keys are supported.
    clone_copy_fields = (
        "study_course",
        "type",
        # Metadata
        "doi",
        "isbn",
        "author",
        "language",
        "publisher",
        "source_format",
        # Matuc settings
        "magsbs_appendix_prefix",
        "magsbs_generate_toc",
        "magsbs_page_numbering_gap",
        "magsbs_toc_depth",
    )

    def clone_view(self, request, pk):
        """View for creating a new course based on an existing one."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        orig = get_object_or_404(self.get_queryset(request), pk=pk)
        if not self.has_view_or_change_permission(request, orig):
            raise PermissionDenied
        # Courses with static material can't be cloned
        if orig.is_static:
            raise PermissionDenied
        opts = self.model._meta
        if request.method == "POST":
            form = self.CloneCourseForm(request.POST, request.FILES)
            # Also catch validation errors not directly related to the form and
            # attach them to the form as well for easier displaying
            try:
                if form.is_valid():
                    obj = form.instance
                    for field_name in self.clone_copy_fields:
                        setattr(obj, field_name, getattr(orig, field_name))
                    obj.creator = request.user
                    obj.full_clean()
                    obj.save()
                    spooled_update_matuc_config(obj.pk)
                    spooled_import_course_repository(orig.pk, obj.pk)
                    return redirect(
                        f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_change",
                        obj.pk,
                    )
            except ValidationError as err:
                form.errors.update(err.message_dict)
                print(form.errors)
        else:
            form = self.CloneCourseForm(
                initial={
                    "name": orig.name,
                    "metadata_audience": orig.metadata_audience,
                    "material_audience": orig.material_audience,
                    "internal_reference": orig.internal_reference,
                }
            )
        ctx = {
            **self.admin_site.each_context(request),
            "opts": opts,
            "orig": orig,
            "form": form,
        }
        return render(request, "admin/course/clone.html", context=ctx)

    def get_fieldsets(self, request, obj=None):
        fieldsets = []
        # Show reduced fieldsets when creating a new course
        if obj is None:
            fieldsets.append(
                (
                    _("Main info"),
                    {
                        "fields": (
                            "name",
                            "slug",
                            "study_course",
                            "term",
                            "type",
                            "internal_reference",
                            "editing_status",
                        )
                    },
                )
            )
        else:
            fieldsets.append(
                (
                    _("Main info"),
                    {
                        "fields": (
                            "name",
                            "slug",
                            "study_course",
                            "term",
                            "type",
                            "internal_reference",
                            "editing_status",
                            "creator",
                            "date_created",
                        )
                    },
                )
            )
        # User could be an editor only, so don't show him things he may not change
        if request.user.is_staff:
            fieldsets.append(
                (
                    _("Metadata"),
                    {
                        "fields": (
                            "doi",
                            "isbn",
                            "author",
                            "language",
                            "publisher",
                            "source_format",
                        ),
                        "classes": () if obj is None else ("collapse",),
                    },
                )
            )
            fieldsets.append(
                (
                    _("Permissions"),
                    {
                        "fields": ("metadata_audience", "material_audience"),
                        "classes": () if obj is None else ("collapse",),
                    },
                )
            )
        # Don't show static material fields for courses with is_static=False
        if obj is None or obj.is_static:
            fieldsets.append(
                (
                    _("Static material"),
                    {
                        "description": _(
                            "Courses with static material will have no repository "
                            "and hence won't support the regular editing workflow. "
                            "Instead, material to be offered for download can be "
                            "provided directly. "
                            "Note that whether a course is to be edited or material "
                            "should be provided statically can't be changed after "
                            "the course was created."
                        ),
                        "fields": ("static_material_upload",),
                        "classes": () if obj is None else ("collapse",),
                    },
                )
            )
        # Build settings can only be changed after creation
        if obj is not None and not obj.is_static:
            fieldsets.append(
                (
                    _("Build settings"),
                    {
                        "fields": (
                            "magsbs_appendix_prefix",
                            "magsbs_generate_toc",
                            "magsbs_page_numbering_gap",
                            "magsbs_toc_depth",
                        ),
                        # Don't collapse for editors
                        "classes": ("collapse",) if request.user.is_staff else (),
                    },
                )
            )
        return tuple(fieldsets)

    def get_inlines(self, request, obj=None):
        # Don't offer inlines when adding a course
        if obj is None:
            return ()
        inlines = [
            self.SubCourseInline,
            self.EasyAccessInline,
            self.CourseStudentSubscriptionInline,
        ]
        # Courses with static material have no editors
        if not obj.is_static:
            inlines.append(self.CourseEditorSubscriptionInline)
        return tuple(inlines)

    def get_queryset(self, request):
        """For non-staff users, restrict to courses the user is editor of."""
        qs = super().get_queryset(request)
        if request.user.is_staff:
            return qs
        return qs.filter(editors=request.user)

    def get_readonly_fields(self, request, obj=None):
        fields = ["creator", "date_created"]
        # Prevent changing fields URLs and paths depend on after creation
        if obj is not None:
            fields.extend(("slug", "study_course", "term", "type"))
        # Editors may only change matuc settings
        if not request.user.is_staff:
            fields.extend(("name", "internal_reference", "editing_status"))
        return tuple(fields)

    def get_urls(self):
        opts = self.model._meta
        return [
            path(
                "<int:pk>/clone/",
                self.admin_site.admin_view(self.clone_view),
                name=f"{opts.app_label}_{opts.model_name}_clone",
            )
        ] + super().get_urls()

    def response_add(self, request, obj, post_url_continue=None):
        """Redirect to change view after creating a course."""
        if "_addanother" not in request.POST and IS_POPUP_VAR not in request.POST:
            request.POST = request.POST.copy()
            request.POST["_continue"] = 1
        return super().response_add(request, obj, post_url_continue)

    def save_model(self, request, obj, form, change):
        # Set current user as creator on creation
        if not change:
            obj.creator = request.user
        super().save_model(request, obj, form, change)
        # Spool matuc config file updating after changing the course
        if not obj.is_static:
            spooled_update_matuc_config(obj.pk)


@admin.register(CourseType, site=admin_site)
class CourseTypeAdmin(rules_admin.ObjectPermissionsModelAdmin):
    fields = ("name", "slug")
    list_display = ("name", "slug")
    ordering = ("name",)
    search_fields = ("name", "slug")

    def get_readonly_fields(self, request, obj=None):
        # Don't allow changing slug after creation
        fields = super().get_readonly_fields(request, obj=obj)
        if obj is None:
            return fields
        return (*fields, "slug")


@admin.register(MaterialBuild, site=admin_site)
class MaterialBuildAdmin(rules_admin.ObjectPermissionsModelAdmin):
    fields = (
        "course",
        "format",
        "revision",
        "status",
        "error_message",
        "date_created",
        "date_done",
    )
    list_display = ("course", "format", "get_short_revision", "status", "date_created")
    list_filter = ("format", "status")
    ordering = ("-date_created",)
    search_fields = ("course__name", "revision")

    def get_short_revision(self, obj):
        return obj.revision[:7]

    get_short_revision.short_description = _("revision")

    def has_add_permission(self, request):
        """Material builds can't be created manually."""
        return False

    def has_change_permission(self, request, obj=None):
        """Material builds can't be changed, only deleted."""
        return False


@admin.register(StudyCourse, site=admin_site)
class StudyCourseAdmin(rules_admin.ObjectPermissionsModelAdmin):
    fields = ("name", "slug")
    list_display = ("name", "slug")
    ordering = ("name",)
    search_fields = ("name", "slug")

    def get_readonly_fields(self, request, obj=None):
        # Don't allow changing slug after creation
        fields = super().get_readonly_fields(request, obj=obj)
        if obj is None:
            return fields
        return (*fields, "slug")


@admin.register(Term, site=admin_site)
class TermAdmin(rules_admin.ObjectPermissionsModelAdmin):
    fields = ("name", "slug", "start_date", "end_date")
    list_display = ("name", "slug", "start_date", "end_date")
    search_fields = ("name", "slug")

    def get_readonly_fields(self, request, obj=None):
        # Don't allow changing slug after creation
        fields = super().get_readonly_fields(request, obj=obj)
        if obj is None:
            return fields
        return (*fields, "slug")


@admin.register(User, site=admin_site)
class UserAdmin(rules_admin.ObjectPermissionsModelAdminMixin, _UserAdmin):
    """
    Overrides the fieldsets and filters of original ``UserAdmin`` to not include
    permission and group-related options, as these both concepts are not used by
    MatShare. The remaining features of Django's ``UserAdmin`` are, however, fine
    and should be retained.
    """

    class UnusablePasswordMixin:
        """
        Mixin for forms with password1 and password2 field. It allows setting unusable
        password by leaving both fields empty. The two fields have to be redefined
        with required=False by the class using this mixin.
        """

        def clean(self):
            cleaned = super().clean()
            pw1 = cleaned.get("password1")
            pw2 = cleaned.get("password2")
            # This case isn't handled by the base class's clean_password2()
            if pw1 and not pw2 or not pw1 and pw2:
                raise ValidationError(
                    self.error_messages["password_mismatch"], code="password_mismatch"
                )
            return cleaned

        def clean_password2(self):
            pw2 = self.cleaned_data.get("password2")
            # The original implementations assume the field to be required and hence
            # would fail validation on an empty value
            if pw2:
                return super().clean_password2()
            return pw2

        def save(self, commit=True):
            user = super().save(commit=False)
            if not self.cleaned_data.get("password2"):
                user.set_unusable_password()
            if commit:
                user.save()
            return user

    class AdminPasswordChangeForm(UnusablePasswordMixin, _AdminPasswordChangeForm):
        password1 = forms.CharField(
            required=False,
            strip=False,
            help_text=_AdminPasswordChangeForm.base_fields["password1"].help_text,
            label=_AdminPasswordChangeForm.base_fields["password1"].label,
            widget=_AdminPasswordChangeForm.base_fields["password1"].widget,
        )
        password2 = forms.CharField(
            required=False,
            strip=False,
            help_text=_AdminPasswordChangeForm.base_fields["password2"].help_text,
            label=_AdminPasswordChangeForm.base_fields["password2"].label,
            widget=_AdminPasswordChangeForm.base_fields["password2"].widget,
        )

    class UserCreationForm(UnusablePasswordMixin, _UserCreationForm):
        # Make fields optional
        password1 = forms.CharField(
            required=False,
            strip=False,
            help_text=_UserCreationForm.base_fields["password1"].help_text,
            label=_UserCreationForm.base_fields["password1"].label,
            widget=_UserCreationForm.base_fields["password1"].widget,
        )
        password2 = forms.CharField(
            required=False,
            strip=False,
            help_text=_UserCreationForm.base_fields["password2"].help_text,
            label=_UserCreationForm.base_fields["password2"].label,
            widget=_UserCreationForm.base_fields["password2"].widget,
        )

    fieldsets = (
        (_("Login info"), {"fields": ("username", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "email")}),
        (
            _("Settings"),
            {
                "fields": (
                    "study_courses",
                    "default_material_notification_frequency",
                    "sources_notification_frequency",
                    "language",
                    "time_zone",
                )
            },
        ),
        (
            _("Permissions"),
            {
                "fields": ("is_active", "is_staff", "is_superuser"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Important dates"),
            {"fields": ("last_login", "date_joined"), "classes": ("collapse",)},
        ),
    )
    add_fieldsets = (
        (
            _("Login info"),
            {
                "description": _(
                    "Leave both password fields empty if the user will authenticate "
                    "via an external service like LDAP."
                ),
                "fields": ("username", "password1", "password2"),
            },
        ),
        (_("Personal info"), {"fields": ("email", "first_name", "last_name")}),
    )
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    filter_horizontal = ("study_courses",)
    list_display = ("username", "first_name", "last_name", "email", "is_staff")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)
    change_form_template = "admin/user/change_form.html"

    def data_report_view(self, request, pk):
        """View that renders a report about data stored about a user."""
        user = get_object_or_404(self.get_queryset(request), pk=pk)
        if not self.has_view_or_change_permission(request, user):
            raise PermissionDenied
        ctx = {
            **self.admin_site.each_context(request),
            "user": user,
            "now": timezone.now(),
        }
        with user.localized():
            return render(request, "admin/user/data_report.html", context=ctx)

    def get_readonly_fields(self, request, obj=None):
        """Prevent username changing and staff from modifying some information."""
        fields = list(super().get_readonly_fields(request, obj=obj))
        if obj is not None:
            fields.append("username")
        if not request.user.is_superuser:
            fields.extend(("is_staff", "is_superuser"))
        fields.extend(("date_joined", "last_login"))
        return tuple(fields)

    def get_urls(self):
        opts = self.model._meta
        return [
            path(
                "<int:pk>/data-report/",
                self.admin_site.admin_view(self.data_report_view),
                name=f"{opts.app_label}_{opts.model_name}_data_report",
            )
        ] + super().get_urls()
