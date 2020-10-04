import datetime
import logging

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.forms import (
    AuthenticationForm as _AuthenticationForm,
    SetPasswordForm,
)
from django.contrib.auth.views import LoginView as _LoginView, LogoutView as _LogoutView
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import html, timezone, translation
from django.utils.crypto import get_random_string
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_control, never_cache
from django.views.generic import TemplateView, View
from django.views.i18n import (
    JavaScriptCatalog as _JavaScriptCatalog,
    set_language as _set_language,
)

from .models import Course, EasyAccess, User
from .utils import MatShareEmailMessage, set_consent


LOGGER = logging.getLogger(__name__)


def set_language(request):
    """Wrapper for ``django.views.i18n.set_language`` that keeps user settings in sync.

    If a user is authenticated, it updates the preferred language stored in his
    account instead of setting a cookie so that the user's choice is bound to his
    account rather than the browser.
    """
    response = _set_language(request)
    if request.user.is_authenticated:
        # Pop cookie from response and store language in user's account instead
        try:
            request.user.language = response.cookies.pop(
                settings.LANGUAGE_COOKIE_NAME
            ).value
        except KeyError:
            # No cookie set, something went wrong in the set_language view
            pass
        else:
            request.user.save()
    return response


class AuthenticationForm(_AuthenticationForm):
    privacy_policy_accepted = forms.BooleanField(
        label=_("I agree to the privacy policy.")
    )


class AuthenticationFormViewMixin:
    """
    Provides an instance of :class:`AuthenticationForm` as ``auth_form`` in the
    template context if the user isn't authenticated already.
    """

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if not self.request.user.is_authenticated:
            ctx["auth_form"] = AuthenticationForm(self.request)
        return ctx


class MatShareViewMixin:
    """
    A mixin adding various common functionality to all views.
    """

    # Set to the desired values in your view implementation or overwrite the get_*
    # methods for dynamic values.
    title = None

    def get_title_parts(self):
        """Tuple of titles to show, dash-separated, in ``<title>`` tag.

        Can be overridden by subclasses to prepend to the tuple of title parts.
        By default, an empty tuple is returned.
        """
        return ()

    def get_title(self):
        """Value that will make up the HTML ``<title>`` tag.

        By default, ``self.title`` is returned if it's not ``None``. Otherwise,
        ``self.get_title_parts()`` is called and the returned parts joined together,
        separated by dashes. The joined string will already be HTML-escaped and
        marked safe.
        """
        if self.title is None:
            return html.mark_safe(
                " &mdash; ".join(
                    html.escape(str(part)) for part in self.get_title_parts()
                )
            )
        return self.title


class CookieConsentView(View):
    """
    Stores a user's consent to using cookies.
    """

    def post(self, request):
        response = HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("home"))
        )
        set_consent(request, response, "cookies", True)
        return response


class EasyAccessActivationView(MatShareViewMixin, TemplateView):
    """
    View shown when an EasyAccess URL is called. It adds the matching token object's
    primary key to the session's ``easy_access`` dict for further access level
    calculation.
    """

    class EmailConfirmationForm(forms.Form):
        """
        Ensures entered e-mail address matches that of the ``EasyAccess`` object.
        """

        email = forms.EmailField(
            label=_("e-mail address"),
            widget=forms.EmailInput(attrs={"autofocus": True}),
        )
        privacy_policy_accepted = forms.BooleanField(
            label=_("I agree to the privacy policy.")
        )

        def __init__(self, *args, easy_access, **kwargs):
            super().__init__(*args, **kwargs)
            self.easy_access = easy_access

        def clean(self):
            super().clean()
            if "email" in self.cleaned_data and (
                self.easy_access is None
                or self.easy_access.email.lower() != self.cleaned_data["email"].lower()
            ):
                raise ValidationError(
                    _("This combination of link and e-mail address is not valid.")
                )

    template_name = "matshare/easy_access_activation.html"

    # This view shows sensitive data and has to be reloaded every time
    @method_decorator(never_cache)
    def dispatch(self, request, token):
        """Resolve token to ``EasyAccess`` object and pass that on instead."""
        try:
            easy_access = (
                EasyAccess.objects.valid().select_related("course").get(token=token)
            )
        except EasyAccess.DoesNotExist:
            easy_access = None
        return super().dispatch(request, easy_access)

    def get(self, request, easy_access):
        if (
            easy_access is None
            or request.session.get("easy_access", {}).get(str(easy_access.course.pk))
            != easy_access.pk
        ):
            # Unauthorized
            form = self.EmailConfirmationForm(easy_access=easy_access)
            return super().get(request, form=form)
        # Authorized, display info about the EasyAccess object
        self.course = easy_access.course
        return super().get(request, easy_access=easy_access)

    def post(self, request, easy_access):
        ea = request.session.get("easy_access", {})
        if request.POST.get("deactivate"):
            try:
                del ea[str(easy_access.course.pk)]
            except KeyError:
                pass
            else:
                request.session.modified = True
                messages.success(request, _("You have signed out."))
            return redirect(request.META.get("HTTP_REFERER") or reverse("home"))
        form = self.EmailConfirmationForm(request.POST, easy_access=easy_access)
        if form.is_valid():
            # Activate EasyAccess in this session and redirect to info page
            ea[str(easy_access.course.pk)] = easy_access.pk
            request.session["easy_access"] = ea
            response = redirect("easy_access_activation", token=easy_access.token)
            # User has accepted privacy policy by proceeding; store consent in cookie
            set_consent(request, response, "privacy", True)
            return response
        # Display form with errors
        return super().get(request, form=form)

    def get_title(self):
        if hasattr(self, "course"):
            return _("Access to {course}").format(course=self.course)
        return _("EasyAccess")


# View differs for anonymous and authenticated users, so better don't cache
@method_decorator(never_cache, name="dispatch")
class HomeView(AuthenticationFormViewMixin, MatShareViewMixin, TemplateView):
    template_name = "matshare/home.html"
    title = _("Home")

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("user_dashboard")
        return super().get(request)


# Cache the catalog to avoid unnecessary reloading
@method_decorator(cache_control(max_age=3600), name="get")
class JavaScriptCatalog(_JavaScriptCatalog):
    pass


@method_decorator(never_cache, name="dispatch")
class LoginView(MatShareViewMixin, _LoginView):
    template_name = "matshare/login.html"
    form_class = AuthenticationForm
    title = _("Sign in")
    is_login = True

    def form_valid(self, form):
        # Store current language in user's account upon first login
        user = form.get_user()
        if not user.language:
            user.language = translation.get_language()
            user.save()
        response = super().form_valid(form)
        # User has accepted privacy policy by logging in; store consent in cookie
        set_consent(self.request, response, "privacy", True)
        return response

    def get_context_data(self, **kwargs):
        """If it's empty, set next to the value of HTTP_REFERER."""
        ctx = super().get_context_data(**kwargs)
        if not ctx.get("next"):
            ctx["next"] = self.request.META.get("HTTP_REFERER", "")
        return ctx


@method_decorator(never_cache, name="dispatch")
class LogoutView(MatShareViewMixin, _LogoutView):
    def dispatch(self, request, *args, **kwargs):
        messages.success(request, _("You have signed out."))
        return super().dispatch(request, *args, **kwargs)


class PasswordResetViewMixin:
    """
    Ensures password reset views are only available when the ``MS_PASSWORD_RESET`` setting is enabled.
    """

    def dispatch(self, request, *args, **kwargs):
        if not settings.MS_PASSWORD_RESET:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


@method_decorator(never_cache, name="dispatch")
class PasswordResetRequestView(PasswordResetViewMixin, MatShareViewMixin, TemplateView):
    """
    View for requesting a password reset link by entering the user's email address.
    """

    class PasswordResetRequestForm(forms.Form):
        username = forms.CharField(
            max_length=150,
            label=_("Username"),
            widget=forms.TextInput(attrs={"autofocus": True}),
        )
        email = forms.EmailField(label=_("E-mail address"))

        def clean(self):
            data = super().clean()
            # Usernames and email addresses are stored lower-case, hence unify them
            data["username"] = data["username"].lower()
            data["email"] = data["email"].lower()
            return data

    template_name = "matshare/password_reset_request.html"
    title = _("Reset password")

    def get(self, request, form=None):
        if form is None:
            form = self.PasswordResetRequestForm()
        return super().get(request, form=form)

    def post(self, request):
        form = self.PasswordResetRequestForm(request.POST)
        if form.is_valid():
            try:
                user = User.objects.select_for_update(of=("self",)).get(
                    username=form.cleaned_data["username"],
                    email=form.cleaned_data["email"],
                    is_active=True,
                )
            except User.DoesNotExist:
                LOGGER.info(
                    "Password reset request for unknown user: username=%r, email=%r",
                    form.cleaned_data["username"],
                    form.cleaned_data["email"],
                )
                pass
            else:
                if (
                    # Disable password resetting for superusers and external users
                    not user.is_superuser
                    and user.has_usable_password()
                    # Prohibit sending another mail before the previous one has expired
                    and (user.password_reset_expiration_date <= timezone.now())
                ):
                    user.password_reset_token = get_random_string(
                        User.password_reset_token.field.max_length
                    )
                    user.password_reset_expiration_date = (
                        timezone.now()
                        + datetime.timedelta(
                            hours=settings.MS_PASSWORD_RESET_EXPIRATION_HOURS
                        )
                    )
                    with user.localized():
                        MatShareEmailMessage(
                            (user.email,),
                            _("Reset password"),
                            "matshare/email/password_reset.html",
                            template_context={"user": user},
                        ).send()
                    user.save()
                    LOGGER.info("Password reset mail sent for %r", user)
                else:
                    LOGGER.info("Password reset request rejected for %r", user)
            return redirect("password_reset_request_sent")
        return super().get(request, form=form)


class PasswordResetRequestSentView(
    PasswordResetViewMixin, MatShareViewMixin, TemplateView
):
    """
    Notifies that a mail was sent.
    """

    template_name = "matshare/password_reset_request_sent.html"
    title = _("Reset password")
    extra_context = {"expiration_hours": settings.MS_PASSWORD_RESET_EXPIRATION_HOURS}


class PasswordResetConfirmView(PasswordResetViewMixin, MatShareViewMixin, TemplateView):
    """
    View for picking a new password after clicking the mailed link.
    """

    template_name = "matshare/password_reset_confirm.html"
    title = _("Reset password")

    @method_decorator(never_cache)
    def dispatch(self, request, pk, token):
        """Fetch the user from database."""
        self.user = get_object_or_404(
            User.objects.select_for_update(of=("self",)),
            pk=pk,
            password_reset_token=token,
            password_reset_expiration_date__gt=timezone.now(),
            is_active=True,
        )
        return super().dispatch(request)

    def get(self, request, form=None):
        if form is None:
            form = SetPasswordForm(self.user)
        return super().get(request, form=form)

    def post(self, request):
        form = SetPasswordForm(self.user, request.POST)
        if form.is_valid():
            form.save()
            self.user.password_reset_token = (
                User.password_reset_token.field.get_default()
            )
            self.user.password_reset_expiration_date = (
                User.password_reset_expiration_date.field.get_default()
            )
            self.user.save()
            LOGGER.info("Password reset successful for %r", self.user)
            messages.success(request, _("Password was reset successfully."))
            return redirect("home")
        return super().get(request, form=form)


class LegalNoticeView(MatShareViewMixin, TemplateView):
    template_name = "matshare/legal_notice.html"
    title = _("Legal notice and privacy policy")


class View404(AuthenticationFormViewMixin, MatShareViewMixin, TemplateView):
    template_name = "matshare/404.html"
    title = _("Page not found")
