from django.conf import settings
from django.utils import timezone


class AccountBasedLocalizationMiddleware:
    """
    Middleware that executes views with language and time zone stored in an
    authenticated user's account.

    It must be listed after ``django.contrib.auth.middleware.AuthenticationMiddleware``
    in ``MIDDLEWARES`` setting.
    Since it only affects authenticated users, it makes sense to use it after
    ``django.middleware.locale.LocaleMiddleware`` to override the language for
    authenticated users, ignoring an eventual language cookie.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            with request.user.localized():
                return self.get_response(request)
        return self.get_response(request)


class ConsentMiddleware:
    """
    Adds a dictionary ``request.consents`` mapping consent names from the ``CONSENTS``
    setting to booleans telling whether the particular consent was given or not. A
    value of ``None`` means the specific consent wasn't asked for yet.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        consents = {name: None for name in settings.CONSENTS}
        for item in request.COOKIES.get("consents", "").split(","):
            try:
                name, version, given = item.split(":", 2)
                version = int(version)
                given = int(given)
            except ValueError:
                # Skip malformed items
                continue
            if name not in settings.CONSENTS:
                # Skip consents which don't exist anymore
                continue
            if version != settings.CONSENTS[name]["version"]:
                # Mark as undecided if the consent was updated to a new version
                continue
            consents[name] = bool(given)
        request.consents = consents
        return self.get_response(request)
