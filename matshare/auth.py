"""
This module contains customized Django authentication backends for use with MatShare.
"""

import logging

from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ValidationError
from django_auth_ldap.backend import LDAPBackend


LOGGER = logging.getLogger(__name__)


class MatShareLDAPBackend(LDAPBackend):
    """
    Customized LDAP authentication backend with some post-authentication checks.
    """

    def authenticate_ldap_user(self, ldap_user, password):
        user = super().authenticate_ldap_user(ldap_user, password)
        if user is not None:
            # If not all attributes listed in AUTH_LDAP_USER_ATTR_MAP were present
            # in LDAP, the user won't be fully populated and hence user saving and
            # login should be prevented in these cases
            try:
                user.full_clean()
            except ValidationError as err:
                LOGGER.warning(
                    "Denied LDAP authentication due to user validation failure: %r", err
                )
                return None
        return user


class MatShareModelBackend(ModelBackend):
    """
    A subclass of Django's ``ModelBackend``, but without the database-backed permission
    checking. Permissions in MatShare are handled by the rules framework, hence
    we can avoid doing extra db queries when rules answers a permission check with
    ``False`` and, consequently, Django continues by asking this backend as well.

    It always answers with ``False``.
    """

    def has_module_perms(self, user, app_label):
        return False

    def has_perm(self, user, perm, obj=None):
        return False
