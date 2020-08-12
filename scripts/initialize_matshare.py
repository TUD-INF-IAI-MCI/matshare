#!/usr/bin/env python3

"""
Script to be run before starting MatShare.

It creates a superuser account if none exists yet.
"""

import os
import sys

import django

django.setup()

from django.conf import settings
from django.urls import reverse

from matshare.models import User


usernames = User.objects.filter(is_superuser=True).values_list("username", flat=True)
if usernames:
    print(len(usernames), "superusers found:", ", ".join(map(repr, usernames)))
    print("Not creating another one.")
    sys.exit()

username = os.getenv("MS_ADMIN_USER", "admin")
password = os.getenv("MS_ADMIN_PASSWORD", "admin")
User.objects.create_superuser(
    username=username,
    password=password,
    email=os.getenv("MS_ADMIN_EMAIL", "very@invalid_email"),
    first_name=os.getenv("MS_ADMIN_FIRST_NAME", "John"),
    last_name=os.getenv("MS_ADMIN_LAST_NAME", "Doe"),
)
print(
    f"""\
A superuser was created with username {username!r} and password {password!r}.
Head over to

    {settings.MS_ROOT_URL}{reverse("admin:index")}

and log in with these credentials to start setting up MatShare.\
"""
)
