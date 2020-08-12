"""
Template context processors.
"""

from django.conf import settings

from . import __version__


def matshare_context_processor(request=None):
    """Add some values commonly used among views.

    The request is optional so that the processor can also be used outside a request.
    """
    from .models import Course, MaterialBuild

    return {
        "CONTACT_EMAIL": settings.MS_CONTACT_EMAIL,
        "MATSHARE_URL": settings.MS_ROOT_URL,
        "MATSHARE_VERSION": __version__,
        "PASSWORD_RESET_ENABLED": settings.MS_PASSWORD_RESET,
        "AccessLevel": Course.AccessLevel,
        "Format": MaterialBuild.Format,
    }
