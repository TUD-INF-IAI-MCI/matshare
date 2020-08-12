"""
Implementations for building material in different formats.
"""

import fnmatch
import glob
import os
import shutil
import subprocess


# Glob patterns that match files/directories serving a specific purpose.
# Only basenames can be matched, so don't try to include slashes in the patterns.
BUILD_ARTIFACT_PATTERNS = ("gladtex.cache",)
MD_PATTERNS = ("*.md",)


class MatucFailed(Exception):
    """
    Raised when matuc fails.
    """

    def __init__(self, returncode, output):
        self.returncode = returncode
        self.output = output
        super().__init__()

    def __repr__(self):
        return (
            f"Matuc failed with returncode {self.returncode}.\n"
            f"Output:\n{self.output}"
        )


def build_epub(build):
    run_matuc("conv", ".", "-f", "epub")
    os.makedirs(build.absolute_path)
    epub_files = glob.glob("*.epub")
    assert len(epub_files) == 1, f"Found more than one epub file: {epub_files[:10]}"
    assert os.path.isfile(epub_files[0]), f"{epub_files[0]!r} is not a file"
    shutil.copyfile(epub_files[0], os.path.join(build.absolute_path, epub_files[0]))


def build_html(build):
    run_matuc("conv", ".", "-f", "html")
    shutil.copytree(
        ".",
        build.absolute_path,
        ignore=ignore_by_patterns(BUILD_ARTIFACT_PATTERNS + MD_PATTERNS),
    )


def ignore_by_patterns(patterns):
    """Return a callable to be passed to ``shutil.copytree`` as ``ignore`` parameter.

    The callable will cause all files/directories matching any of the given glob
    patterns to be ignored. The patterns can only match basenames, thus they may
    not contain slashes.
    """

    def _filter_names(src_dir, names):
        return [
            name
            for name in names
            for pattern in patterns
            if fnmatch.fnmatchcase(name, pattern)
        ]

    return _filter_names


def run_matuc(*args):
    """Runs the matuc command with given arguments.

    :raises MatucFailed: if the process exits with a non-zero code
    """
    result = subprocess.run(
        ["matuc", *args],
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        raise MatucFailed(result.returncode, result.stdout)
