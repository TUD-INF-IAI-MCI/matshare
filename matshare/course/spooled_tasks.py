import datetime
import logging
import os
import posixpath
import shutil
import tempfile

from django.conf import settings
from django.db import transaction
from django.utils import timezone
import pygit2

from .. import material_building
from ..git import utils as git_utils
from ..models import Course, MaterialBuild
from ..spooled_tasks import spooled_task


LOGGER = logging.getLogger(__name__)


@spooled_task(at=datetime.timedelta(seconds=1), retry_count=3, retry_timeout=10)
def spooled_build_material(build_pk):
    """Performs material building for :class:`MaterialBuild` with given pk."""
    qs = (
        MaterialBuild.objects.with_prefetching().filter(pk=build_pk)
        # Lock the MaterialBuild object until the transaction ends
        .select_for_update(of=("self",))
    )

    # Ensure we are the first one trying to build this
    with transaction.atomic():
        build = qs.get()
        if build.status != MaterialBuild.Status.waiting:
            LOGGER.warning("MaterialBuild already locked: %r", build)
            return
        build.status = MaterialBuild.Status.building
        build.save()

    # Now, no one else will start building this
    LOGGER.info("Building material: %r", build)
    with transaction.atomic():
        build = qs.get()
        try:
            builder = getattr(material_building, "build_" + build.format.name)
            # Not create the final directory, so that shutil.copytree() can be used
            os.makedirs(os.path.dirname(build.absolute_path), exist_ok=True)
            # Pull the edited material into a temporary directory as scratchpad
            repo = pygit2.Repository(build.course.absolute_repository_path)
            browser = git_utils.ContentBrowser(repo, settings.MS_GIT_MAIN_REF)
            with tempfile.TemporaryDirectory() as tmpdir:
                # Create an inner working directory because matuc sometimes pollutes
                # the parent directory
                scratchdir = os.path.join(tmpdir, "build")
                os.makedirs(scratchdir)
                browser.write_to_fs(scratchdir, settings.MS_GIT_EDIT_SUBDIR)
                os.chdir(scratchdir)
                # Open another transaction block so that Django can roll back and
                # leave in a clean state if anything goes wrong, still allowing to
                # mark the build failed
                with transaction.atomic():
                    builder(build)
            # Ensure the build results directory exists
            os.makedirs(build.absolute_path, exist_ok=True)
            # Fix permissions that were preserved from temporary directory
            os.chmod(build.absolute_path, 0o755)
        except Exception as err:
            build.status = MaterialBuild.Status.failed
            build.error_message = repr(err)
            LOGGER.exception("Failed to build material: %r", build)
        else:
            build.status = MaterialBuild.Status.completed
        finally:
            build.date_done = timezone.now()
            build.save()


@spooled_task(at=datetime.timedelta(seconds=1), retry_count=3, retry_timeout=10)
def spooled_import_course_repository(src_course_pk, dest_course_pk):
    """Updates the repository of a course with the contents of another one."""
    with transaction.atomic():
        dest_course = Course.objects.select_for_update(of=("self",)).get(
            pk=dest_course_pk
        )
        src_course = Course.objects.get(pk=src_course_pk)
        src_repo = pygit2.Repository(src_course.absolute_repository_path)
        dest_repo = pygit2.Repository(dest_course.absolute_repository_path)
        browser = git_utils.ContentBrowser(dest_repo, settings.MS_GIT_MAIN_REF)
        browser.add_from_other_repo(
            src_repo,
            settings.MS_GIT_MAIN_REF,
            exclude=(
                # Don't copy the matuc config
                posixpath.normpath(
                    posixpath.join(
                        settings.MS_GIT_EDIT_SUBDIR, settings.MS_MATUC_CONFIG_FILE
                    )
                ),
            ),
        )
        commit_id = browser.commit(
            git_utils.create_admin_signature(),
            f"Import from {src_course}",
            settings.MS_GIT_MAIN_REF,
        )
        # Update revisions to trigger builds and editor notifications
        dest_course.mark_material_updated(commit_id.hex)
        dest_course.mark_sources_updated(commit_id.hex)
        dest_course.save()


@spooled_task(at=datetime.timedelta(seconds=1), retry_count=3, retry_timeout=10)
def spooled_update_matuc_config(course_pk):
    """Updates matuc configuration file in repository if its content has changed."""
    with transaction.atomic():
        course = Course.objects.select_for_update(of=("self",)).get(pk=course_pk)
        content = course.generate_matuc_config()
        config_file = posixpath.normpath(
            posixpath.join(settings.MS_GIT_EDIT_SUBDIR, settings.MS_MATUC_CONFIG_FILE)
        )
        repo = pygit2.Repository(course.absolute_repository_path)
        browser = git_utils.ContentBrowser(repo, settings.MS_GIT_MAIN_REF)
        try:
            existing_id = browser[config_file].id
        except KeyError:
            # File wasn't present
            pass
        else:
            if existing_id == pygit2.hash(content):
                # File is unchanged
                return
        browser.add_from_bytes(config_file, content)
        commit_id = browser.commit(
            git_utils.create_admin_signature(),
            "Updated metadata",
            settings.MS_GIT_MAIN_REF,
        )
        # Update revision to trigger builds
        course.mark_material_updated(commit_id.hex)
        course.save()
