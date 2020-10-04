"""
Template context processors.
"""

from django.conf import settings

from . import __version__


def easy_access_context_processor(request):
    """
    Add EasyAccess objects for displaying in top menu.
    """
    from .models import EasyAccess

    ea_pks = tuple(request.session.get("easy_access", {}).values())
    return {"easy_accesses": EasyAccess.objects.filter(pk__in=ea_pks) if ea_pks else ()}


def matshare_context_processor(request=None):
    """Add some values commonly used among views.

    The request is optional so that the processor can also be used outside a request.
    """
    from .models import Course, MaterialBuild

    return {
        "CONTACT_EMAIL": settings.MS_CONTACT_EMAIL,
        "MATSHARE_ROOT_URL": settings.MS_ROOT_URL,
        "MATSHARE_VERSION": __version__,
        "PASSWORD_RESET_ENABLED": settings.MS_PASSWORD_RESET,
        "AccessLevel": Course.AccessLevel,
        "Format": MaterialBuild.Format,
    }
