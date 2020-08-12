import base64
import functools
import os
import shutil

from django import forms
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.db import models
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django_auth_ldap.config import LDAPGroupQuery
import django_filters
import stdnum
import stdnum.isbn

from .context_processors import matshare_context_processor


def basic_auth(func=None, realm="", auth_backend=None, max_header_size=None):
    """View decorator that performs HTTP Basic Authentication against Django."""
    # Simply strip out quotes and backslashes to avoid escaping
    realm = realm.replace('"', "").replace("\\", "")
    if func is None:
        return functools.partial(
            basic_auth,
            realm=realm,
            auth_backend=auth_backend,
            max_header_size=max_header_size,
        )

    @functools.wraps(func)
    def _basic_auth_wrapper(request, *args, **kwargs):
        user = None
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        if auth_header:
            if max_header_size is not None and len(auth_header) > max_header_size:
                # Return 413 Request Entity Too Large
                return HttpResponse(status=413)
            try:
                scheme, b64_credentials = auth_header.split()
                assert scheme.lower() == "basic"
                credentials = base64.b64decode(b64_credentials).decode()
                username, password = credentials.split(":", 1)
            except (AssertionError, ValueError):
                return HttpResponse(status=400)
            user = authenticate(
                request, username=username, password=password, backend=auth_backend
            )
        if user is None:
            response = HttpResponse(status=401)
            response["WWW-Authenticate"] = f'Basic realm="{realm}"'
            return response
        # Pass user to view
        return func(request, user, *args, **kwargs)

    return _basic_auth_wrapper


def parse_ldap_group_query_string(query_string):
    """Parse OpenLDAP search filter-like strings, but for group queries instead.

    An example of a parseable string is::

        "|(cn=admins,ou=...)(&(cn=students,ou=...)(~(cn=first_term,ou=...)))"

    :return django_auth_ldap.config.LDAPGroupQuery:
    """
    stack = []
    operand = LDAPGroupQuery()
    operand_name = ""
    binary_operator_ok = True
    negate_next = False
    for idx, char in enumerate(query_string):
        if char == "(":
            # No opening parenthesis inside a name
            if operand_name:
                raise ValueError(f"Unexpected {char!r} at position {idx+1}")
            # Also store the position at which the parenthesis was opened for eventual
            # error messages about unclosed parentheses
            stack.append((operand, idx))
            operand = LDAPGroupQuery()
            if negate_next:
                operand.negate()
            binary_operator_ok = True
            negate_next = False
            operand_name = ""
        elif char == ")":
            if operand_name:
                operand.add(operand_name.rstrip(), operand.connector)
                operand_name = ""
            try:
                outer_operand, _ = stack.pop()
            except IndexError:
                raise ValueError(
                    f"Closing parenthesis without prior opening at position {idx+1}"
                ) from None
            outer_operand.add(operand, outer_operand.connector)
            operand = outer_operand
            binary_operator_ok = False
        elif char in "~&|":
            # The binary operators may only go directly after an opening parenthesis
            if not binary_operator_ok or operand_name or operand:
                raise ValueError(f"Unexpected {char!r} at position {idx+1}")
            if char == "~":
                negate_next = not negate_next
            elif char == "&":
                operand.connector = LDAPGroupQuery.AND
            elif char == "|":
                operand.connector = LDAPGroupQuery.OR
            binary_operator_ok = False
        else:
            # Ignore whitespace at the beginning
            if not operand_name and not char.strip():
                continue
            # Names may only go into empty operands and may not start after a binary
            # operator without another opening parenthesis in between
            if operand or not operand_name and not binary_operator_ok:
                raise ValueError(f"Unexpected {char!r} at position {idx+1}")
            operand_name += char
            binary_operator_ok = False
    if stack:
        raise ValueError(f"Unclosed parenthesis at position {stack[0][1]+1}")
    # Handle a single plain name without any parentheses at all
    if operand_name:
        operand.add(operand_name.rstrip(), operand.connector)
    return operand


def rmtree_and_clean(path, clean_up_to):
    """Recursively remove the directory ``path`` and clean up empty parent directories.

    ``clean_up_to`` has to be a parent directory of ``path``. Empty
    directories are then removed up to ``clean_up_to``, but not ``clean_up_to`` itself.
    """
    path = os.path.abspath(path)
    clean_up_to = os.path.abspath(clean_up_to)
    if not path.startswith(clean_up_to + os.sep):
        raise ValueError(f"{clean_up_to!r} is not a parent of {path!r}")
    shutil.rmtree(path)
    to_clean = path
    while True:
        to_clean = os.path.dirname(to_clean)
        # Stop at clean_up_to
        if to_clean == clean_up_to:
            break
        try:
            os.rmdir(to_clean)
        except OSError:
            # Directory not empty, stop cleaning
            break


def set_consent(request, response, consent_name, consent_given):
    """Update the state for a consent by (re)setting the consent cookie.

    The corresponding value in ``request.consents`` is updated as well.

    :param request: the current request object
    :type  request: django.http.HttpRequest
    :param response: a pre-created response object
    :type  response: django.http.HttpResponse
    :param consent_name: name of the consent to update as in ``CONSENTS`` setting
    :type  consent_name: str
    :param consent_given:
        whether the consent was given (boolean) or ``None`` to remove the decision
    :type  consent_given: bool, None
    :raise ValueError:
        if a non-existent ``consent_name`` was given or the type of ``consent_given``
        is invalid
    """
    if consent_name not in settings.CONSENTS:
        raise ValueError(f"No such consent configured: {consent_name!r}")
    if consent_given not in (True, False, None):
        raise ValueError(f"Invalid value to set: {consent_given!r}")
    request.consents[consent_name] = consent_given
    items = [
        f"{name}:{settings.CONSENTS[name]['version']}:{given:d}"
        for name, given in request.consents.items()
        if given is not None
    ]
    items.sort()
    value = ",".join(items)
    # Only set the cookie when its value has changed
    if value != request.COOKIES.get("consents"):
        response.set_cookie("consents", value, max_age=60 * 60 * 24 * 365)


class IntegerEnumField(models.PositiveSmallIntegerField):
    def __init__(self, enum, *args, exclude=(), **kwargs):
        self.enum = enum
        self.exclude = exclude
        kwargs["choices"] = [c for c in enum.choices if c[0] not in exclude]
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        del kwargs["choices"]
        args.insert(0, self.enum)
        if self.exclude:
            kwargs["exclude"] = self.exclude
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return self.enum(value)

    def to_python(self, value):
        if value is None or isinstance(value, self.enum):
            return value
        return self.enum(super().to_python(value))


class ISBNField(models.CharField):
    """
    Model field that validates and stores ISBN-13 numbers.

    Any dashes and spaces are removed as part of validation before ISBNs are
    stored. ISBN-10 numbers are converted to ISBN-13 unless ``convert_isbn10=False``
    is given.

    An additional property is added to instances of models having this field under
    the name ``"<fieldname>_dashed"``, providing the current value in human-readable
    dashed notation.
    """

    class ISBNInput(forms.TextInput):
        """
        Text input widget that shows ISBNs in dashed notation.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.attrs.setdefault("placeholder", "XXX-X-XXXXX-XXX-X")

        def format_value(self, value):
            try:
                return "-".join(stdnum.isbn.split(stdnum.isbn.validate(value)))
            except stdnum.exceptions.ValidationError:
                return value

    description = "ISBN"

    def __init__(self, convert_isbn10=True, **kwargs):
        self.convert_isbn10 = convert_isbn10
        kwargs.setdefault("max_length", 13)
        kwargs.setdefault("verbose_name", self.description)
        super().__init__(**kwargs)

    def contribute_to_class(self, cls, name, **kwargs):
        super().contribute_to_class(cls, name, **kwargs)
        # Provide a <fieldname>_dashed property to generate the ISBN in dashed notation
        dashed_name = f"{name}_dashed"
        if not hasattr(cls, dashed_name):

            def _get_dashed(model_instance):
                value = getattr(model_instance, name)
                if value in (None, ""):
                    return value
                return "-".join(stdnum.isbn.split(value))

            setattr(cls, dashed_name, property(_get_dashed))

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if kwargs["verbose_name"] == self.description:
            del kwargs["verbose_name"]
        if not self.convert_isbn10:
            kwargs["convert_isbn10"] = False
        return name, path, args, kwargs

    def formfield(self, **kwargs):
        kwargs = {
            "min_length": 10,
            # Permit 50 extra characters to cope with spaces pasted accidentally
            "max_length": kwargs.get("max_length", self.max_length) + 50,
            **kwargs,
            # Force widget to be the custom ISBNInput
            "widget": self.ISBNInput(),
        }
        return super().formfield(**kwargs)

    def get_prep_value(self, value):
        # Ensure ISBN is stored without dashes
        if value not in (None, ""):
            value = stdnum.isbn.validate(value, convert=self.convert_isbn10)
        return super().get_prep_value(value)

    def to_python(self, value):
        # Validate ISBN and clean out dashes and spaces
        if value in (None, ""):
            return value
        value = super().to_python(value)
        try:
            return stdnum.isbn.validate(value, convert=self.convert_isbn10)
        except stdnum.exceptions.InvalidLength:
            raise ValidationError(_("The number of digits is wrong."))
        except stdnum.exceptions.ValidationError:
            raise ValidationError(
                _("The checksum is invalid. You probably mistyped the number.")
            )


class MatShareFilterSet(django_filters.FilterSet):
    """
    Base for all filter sets that adds ordering and pagination.
    """

    # Number of ordering filters to show
    ordering_depth = 3

    # Whether to add pagination support
    pagination = True
    page_sizes = (25, 50, 75, 100)
    default_page_size = 25

    def __init__(self, data=None, *args, **kwargs):
        # Create a mutable copy of the submitted form data to allow manipulating it
        if data is not None:
            data = data.copy()

        super().__init__(data=data, *args, **kwargs)

        # Separate special fields from the main filter form
        self.meta_form = forms.Form(data=self.form.data, prefix=self.form.prefix)

        # Allow filtering for specific pk's
        self.meta_form.fields["pk"] = TypedMultipleValueField(
            coerce=int, required=False,
        )

        # Allow ticking a subset of the rows explicitly
        self.meta_form.fields["tick"] = TypedMultipleValueField(
            coerce=int, required=False,
        )

        if "o" in self.filters and self.ordering_depth:
            for index in range(1, self.ordering_depth + 1):
                self.meta_form.fields["o_{}".format(index)] = forms.ChoiceField(
                    choices=self.form.fields["o"].choices,
                    required=False,
                    label="Order by {}.".format(index),
                )

        if self.pagination:
            assert self.default_page_size in self.page_sizes
            self.meta_form.fields["page_size"] = forms.ChoiceField(
                choices=((size, size) for size in self.page_sizes),
                required=False,
                widget=forms.HiddenInput(),
            )
            self.meta_form.fields["page"] = forms.IntegerField(
                min_value=1, required=False, widget=forms.HiddenInput(),
            )

        # Helper field which is submitted when the filters should be cleared
        self.meta_form.fields["reset"] = forms.BooleanField(
            required=False, widget=forms.HiddenInput(),
        )
        if self.meta_form["reset"].data:
            # Clear out all query data related to this filterset
            for name in self.form.fields:
                self.form.data.pop(self.form[name].html_name, None)
            for name in self.meta_form.fields:
                if name == "panel_open":
                    continue
                self.meta_form.data.pop(self.meta_form[name].html_name, None)

        # Helper fields for selecting/deselecting all items across pages
        self.meta_form.fields["select_all"] = forms.BooleanField(
            required=False, widget=forms.HiddenInput(),
        )
        self.meta_form.fields["deselect_all"] = forms.BooleanField(
            required=False, widget=forms.HiddenInput(),
        )

    def filter_queryset(self, queryset):
        """Does some filtering on the queryset not related to a particular filter.

        it patches the ordering filter's value to support deep ordering.
        """
        self.meta_form.is_valid()
        pks = self.meta_form.cleaned_data.get("pk")
        if pks:
            queryset = queryset.filter(pk__in=pks)

        if "o" in self.filters:
            values = []
            for field in self.deep_ordering_fields:
                value = self.meta_form.cleaned_data.get(field.name)
                if value:
                    values.append(value)
            self.form.cleaned_data["o"] = values

        return super().filter_queryset(queryset)

    @cached_property
    def ticked_qs(self):
        """Returns self.qs, restricted to only ticked objects.

        If none are ticked, an empty QuerySet is returned.
        """
        qs = self.qs
        self.meta_form.is_valid()
        if self.meta_form.cleaned_data["select_all"]:
            # Simulate all objects being ticked
            return qs
        if not self.meta_form.cleaned_data["deselect_all"]:
            pks = self.meta_form.cleaned_data["tick"]
            if pks:
                return qs.filter(pk__in=pks)
        # None ticked, more performant than qs.filter(pk__in=[])
        return qs.none()

    @cached_property
    def out_of_page_ticks(self):
        """Returns an iterable of ticked pk's on other pages."""
        if self.page is None:
            return ()
        page_pks = (obj.pk for obj in self.page)
        return (
            pk
            for pk in self.ticked_qs.values_list("pk", flat=True)
            if pk not in page_pks
        )

    @cached_property
    def deep_ordering_fields(self):
        """Returns a tuple of bound deep-ordering form fields of this filterset."""
        return tuple(
            self.meta_form["o_{}".format(index)]
            for index in range(1, self.ordering_depth + 1)
        )

    @cached_property
    def min_page_size(self):
        """Returns the minimum selectable page size for use in templates."""
        if self.page_sizes:
            return min(self.page_sizes)
        return 0

    @cached_property
    def page(self):
        """Returns the current Page object or None, if pagination is disabled."""
        paginator = self.paginator
        if paginator is None:
            return None
        self.meta_form.is_valid()
        return paginator.get_page(self.meta_form.cleaned_data.get("page"))

    @cached_property
    def paginator(self):
        """Creates and returns a Paginator object for the current queryset.

        It returns None if pagination has been disabled via the pagination attribute.
        """
        if not self.pagination:
            return None
        page_size = self.default_page_size
        if self.meta_form.is_valid():
            page_size = int(self.meta_form.cleaned_data["page_size"] or page_size)
        return Paginator(self.qs, page_size)


class MatShareEmailMessage(EmailMessage):
    """
    Custom, simplified subclass of :class:`EmailMessage` that sets some defaults.
    """

    # Send text/html mails by default
    content_subtype = "html"

    def __init__(
        self, to, subject, template_name, template_context=None, from_email=None
    ):
        context = matshare_context_processor()
        if template_context:
            context.update(template_context)
        body = render_to_string(template_name, context)
        super().__init__(
            subject=subject,
            body=body,
            from_email=from_email,
            to=to,
            reply_to=(settings.MS_CONTACT_EMAIL,),
        )


class TypedMultipleValueField(forms.TypedMultipleChoiceField):
    """
    A field which can store multiple values sent in a query under the same name.
    Each value is passed through the given coerce() function as part of the cleaning
    process, which should raise a ValueError or ValidationError for invalid values.
    Other than TypedMultipleChoiceField, this field offers no set of choices to pick
    values from. The only validation a value has to pass in order to be accepted is
    the coerce() function.
    If no coerce function is given, values are left as strings.
    The default widget is a SelectMultiple, but with input_type set to hidden.
    """

    def __init__(self, **kwargs):
        if "choices" in kwargs:
            raise TypeError(f"{self.__class__.__name__} accepts no choices parameter")
        kwargs["choices"] = ()
        default_widget = kwargs.get("widget") is None
        super().__init__(**kwargs)
        if default_widget:
            self.widget.input_type = "hidden"

    def valid_value(self, value):
        """Always return ``True`` since the coerce function is the only validator."""
        return True
