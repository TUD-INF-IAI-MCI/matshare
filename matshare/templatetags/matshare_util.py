import datetime
import uuid

from django import template
from django.utils import html, timezone
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _


register = template.Library()


@register.filter(name="addstr", is_safe=True)
def do_addstr(value, arg):
    """Concatenates two values after coercing them to str."""
    return str(value) + str(arg)


@register.filter(name="boolstr")
def do_boolstr(value):
    """Convert ``bool`` to ``"true"`` or ``"false"`` for use in HTML attributes."""
    return "true" if value else "false"


@register.filter(name="filesize")
def do_filesize(value):
    """Formats a file size given in bytes using an appropriate suffix."""
    if value < 2 ** 10:
        return f"{value} B"
    if value < 2 ** 20:
        return f"{value/2**10:.0f} KiB"
    if value < 10 * 2 ** 20:
        return f"{value/2**20:.1f} MiB"
    return f"{value/2**20:.0f} MiB"


@register.filter(name="git_commit_message")
def do_git_commit_message(value):
    """Format a git commit message as HTML."""
    value = value.strip()
    if not value:
        return _("No message provided")
    lines = value.splitlines()
    if len(lines) > 1 and not lines[1]:
        del lines[1]
    tokens = ["<strong>", html.escape(lines[0].strip()), "</strong>"]
    if len(lines) > 1:
        tokens.extend(
            (
                '<pre style="margin:0 0 0 5px">',
                html.escape("\n".join(lines[1:]).rstrip()),
                "</pre>",
            )
        )
    return mark_safe("".join(tokens))


@register.filter(name="git_short_rev")
def do_git_short_rev(value):
    """Shorten a git revision to the first 7 characters."""
    return value[:7]


@register.filter(name="if_unset")
def do_if_unset(value, arg):
    """If value is None or the empty string, it returns the default, else value.

    This is useful for providing a default for an undefined variable.
    """
    return arg if value in (None, "") else value


@register.filter(name="invert")
def do_invert(value):
    """Return ``not value``."""
    return not value


@register.filter(name="or_dash")
def do_or_dash(value, dash_html="&mdash;"):
    """Return a dash representation if ``value`` is ``None`` or the empty string."""
    return mark_safe(dash_html) if value in (None, "") else value


@register.filter(name="reltime")
def do_reltime(value):
    """Build a relative time representation.

    Given must be a :class:`datetime.datetime` or :class:`datetime.date` object. The
    result looks like "5 min ago" or "2 weeks ago". If ``None`` is given, it returns
    "never". All strings are localized.
    """
    if value is None:
        return _("never")
    now = timezone.localtime()
    if isinstance(value, datetime.datetime):
        if value > now:
            return value
        delta = now - value
        secs = delta.total_seconds()
        if secs < 90:
            return _("{seconds} sec ago").format(seconds=round(secs))
        if secs <= 2 * 60 * 60:
            return _("{minutes} min ago").format(minutes=round(secs / 60))
        if delta <= datetime.timedelta(days=2):
            return _("{hours} hours ago").format(hours=round(secs / 60 / 60))
        # For larger spans, fall back to date handling
        value = value.date()
    if isinstance(value, datetime.date):
        days = (now.date() - value).days
        if days == -1:
            return _("tomorrow")
        if days < 0:
            return _("in {days} days").format(days=-days)
        if days == 0:
            return _("today")
        if days == 1:
            return _("yesterday")
        if days < 28:
            return _("{days} days ago").format(days=days)
        if days < 90:
            return _("{weeks} weeks ago").format(weeks=round(days / 7))
        if days < 3 * 365:
            return _("{months} months ago").format(months=round(days / 30))
        return _("{years} years ago").format(years=round(days / 365))
    raise TypeError(f"{value!r} is no datetime or date object")


@register.filter(name="strip", is_safe=True)
def do_strip(value):
    """Strips leading and trailing whitespace using ``str.strip``."""
    return value.strip()


@register.tag(name="as")
def do_as(parser, token):
    """Stores the content between as and endas in a named variable."""

    try:
        tag_name, var_name = token.split_contents()
    except ValueError:
        raise template.TemplateSyntaxError(
            "{!r} tag requires a single argument".format(token.contents.split()[0])
        )
    nodelist = parser.parse(("endas",))
    parser.delete_first_token()
    return AsNode(var_name, nodelist)


@register.simple_tag(name="uuid")
def do_uuid():
    """Inserts a random UUID4, e.g. for creating HTML IDs dynamically."""
    return str(uuid.uuid4())


class AsNode(template.Node):
    def __init__(self, var_name, nodelist):
        self.var_name = var_name
        self.nodelist = nodelist

    def render(self, context):
        context[self.var_name] = mark_safe(self.nodelist.render(context))
        return ""
