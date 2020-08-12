"""
Django signal handlers that react to model CRUD actions.
They're registered in :meth:`matshare.apps.MatShareConfig.ready`.
"""

import logging
import os

from django.conf import settings
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone, translation
import pygit2

from . import utils
from .course.spooled_tasks import spooled_build_material
from .git import utils as git_utils
from .models import Course, MaterialBuild


LOGGER = logging.getLogger(__name__)


@receiver(post_save, sender=MaterialBuild)
def build_material(sender, instance, created, **kwargs):
    """Spools material building after a new :class:`MaterialBuild` was created."""
    if created:
        spooled_build_material(instance.pk)


@receiver(post_save, sender=Course)
# Ensure commit signatures are not related to the current user's language/time zone
@timezone.override(settings.TIME_ZONE)
@translation.override(settings.LANGUAGE_CODE)
def create_course_repository(sender, instance, created, **kwargs):
    """Initializes the git repository upon course creation.

    The new repository is initialized with contents of ``MS_GIT_INITIAL_DIR``.
    """
    # Do nothing for courses with static material
    if not created or instance.is_static:
        return
    repo_path = instance.absolute_repository_path
    if os.path.isdir(repo_path):
        LOGGER.warning(
            "Created course %r with existing repository %r, skipping initialization",
            instance,
            repo_path,
        )
        return
    LOGGER.info("Creating course repository for %r in %r", instance, repo_path)
    repo = pygit2.init_repository(
        repo_path,
        bare=True,
        flags=(
            # Create all sub-directories down to the repo automatically
            pygit2.GIT_REPOSITORY_INIT_MKPATH
            # Will cause a ValueError when repo already exists
            | pygit2.GIT_REPOSITORY_INIT_NO_REINIT
        ),
    )
    # Keep hooks at a central place instead of copying them to each repository
    repo.config["core.hooksPath"] = settings.MS_GIT_HOOKS_DIR
    # Apply custom git config
    for key, value in settings.MS_GIT_EXTRA_CONFIG.items():
        repo.config[key] = value
    # Commit contents of MS_GIT_INITIAL_DIR as initial commit
    browser = git_utils.ContentBrowser(repo)
    browser.add_from_fs(settings.MS_GIT_INITIAL_DIR)
    browser.commit(
        git_utils.create_admin_signature(), "Initial commit", settings.MS_GIT_MAIN_REF,
    )


@receiver(post_delete, sender=Course)
def remove_course_directories(sender, instance, **kwargs):
    """Remove associated directories after a course was deleted."""
    if instance.is_static:
        dir_to_remove = instance.absolute_static_material_path
        clean_up_to = settings.MEDIA_ROOT
    else:
        dir_to_remove = instance.absolute_repository_path
        clean_up_to = settings.MS_GIT_ROOT
    if not os.path.isdir(dir_to_remove):
        LOGGER.warning("Directory %r doesnn't exist, not removing it", dir_to_remove)
        return
    LOGGER.info("Removing directory %r", dir_to_remove)
    utils.rmtree_and_clean(dir_to_remove, clean_up_to)


@receiver(post_delete, sender=MaterialBuild)
def remove_material_build_directory(sender, instance, **kwargs):
    if not os.path.exists(instance.absolute_path):
        return
    LOGGER.info("Removing material build directory %r", instance.absolute_path)
    try:
        utils.rmtree_and_clean(instance.absolute_path, settings.MEDIA_ROOT)
    except OSError as err:
        LOGGER.warning("Failed to remove %r: %r", instance.absolute_path, err)
