import datetime
import os
import posixpath
import re

from django.conf import settings
from django.utils import timezone
import pygit2


# Target of a non-existent reference, for both SHA-1 and upcoming SHA-256
NULL_REFS = (40 * "0", 64 * "0")

# Matches valid SHA-1 or SHA-256 git object ids, lower-case only
OID_PATTERN = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


def create_admin_signature():
    """Returns a :class:`pygit2.Signature` object to use for administrative commits."""
    return create_signature("MatShare", settings.MS_GIT_ADMIN_EMAIL)


def create_signature(name, email):
    """Creates a :class:`pygit2.Signature` object with current time."""
    now = timezone.localtime()
    return pygit2.Signature(
        name, email, int(now.timestamp()), int(now.utcoffset().total_seconds() / 60)
    )


def extract_commit_info(commit):
    """Extract metadata from given :class:`pygit2.Commit` for displaying."""
    return {
        "author_email": commit.author.email,
        "author_name": commit.author.name,
        "author_time": datetime.datetime.fromtimestamp(commit.author.time).astimezone(
            datetime.timezone(datetime.timedelta(minutes=commit.author.offset))
        ),
        "message": commit.message,
    }


def paths_changed(commit1, commit2, *paths):
    """Inspects the difference between two commits.

    ``commit1`` and ``commit2`` must be of type :class:`pygit2.Commit`.
    For each of the given ``paths``, it yields a boolean telling whether the path
    (or, if it's a directory, a file therein) has changed.
    """
    diff = commit1.tree.diff_to_tree(commit2.tree)
    for path in paths:
        path = posixpath.normpath(path)
        # If the repository root was given, any change is sufficient
        if path == ".":
            try:
                next(diff.deltas)
            except StopIteration:
                yield False
            else:
                yield True
            continue
        dir_prefix = path + "/"
        for delta in diff.deltas:
            if delta.new_file.path == path or delta.new_file.path.startswith(
                dir_prefix
            ):
                yield True
                break
        else:
            yield False


def resolve_committish(repo, committish):
    """Resolves a committish to :class:`pygit2.Commit` object.

    Committish might be one of:

    *  :class:`pygit2.Reference` object (direct or symbolic)
    *  :class:`pygit2.Tag` object
    *  :class:`pygit2.Oid` object of a commit or tag
    *  string that's being resolved using ``pygit2.Repository.resolve_refish``
    *  :class:`pygit2.Commit` object to be returned back directly

    ``TypeError`` is raised if an invalid object type is given as committish,
    ``KeyError`` if the commit couldn't be found.
    """
    if isinstance(committish, pygit2.Commit):
        return committish
    if isinstance(committish, (pygit2.Reference, pygit2.Tag)):
        return committish.peel()
    if isinstance(committish, pygit2.Oid):
        # Oid might be of a commit already, hence we peel for Commit explicitly
        return repo[committish].peel(pygit2.Commit)
    if isinstance(committish, str):
        return repo.resolve_refish(committish)[0]
    raise TypeError(f"committish must be string, Oid or Reference, not {committish!r}")


def walk_pairwise(repo, start_committish, end_committish=None):
    """Walks backwards from ``start_committish`` to ``end_committish``.

    Each time, two values are yielded:

    *  the parent :class:`pygit2.Commit` object
    *  the child :class:`pygit2.Commit` object

    If ``end_committish`` is not given, the full tree is walked.
    """
    start_commit = resolve_committish(repo, start_committish)
    end_commit = (
        None if end_committish is None else resolve_committish(repo, end_committish)
    )
    child_commit = None
    for commit in repo.walk(start_commit.id, pygit2.GIT_SORT_TOPOLOGICAL):
        if child_commit is not None:
            yield commit, child_commit
        if end_commit is not None and commit.id == end_commit.id:
            break
        child_commit = commit


class ContentBrowser:
    """
    Convenience class that provides different means of browsing and updating
    repository contents.
    """

    def __init__(self, repo, base_committish=None):
        self.repo = repo
        self.index = pygit2.Index()
        self.load_base(base_committish)

    def __getitem__(self, path):
        """Allows querying entries from ``self.index`` by path."""
        return self.index[path]

    def add_from_bytes(self, path, content, mode=pygit2.GIT_FILEMODE_BLOB):
        """Add given content (bytes object) to index under given path."""
        self.index.add(pygit2.IndexEntry(path, self.repo.create_blob(content), mode))

    def add_from_fs(self, dir_to_add, prefix=""):
        """Add all files under dir_to_add to index recursively."""
        for root, dirnames, filenames in os.walk(dir_to_add):
            if filenames:
                rel_root = os.path.relpath(root, dir_to_add)
            for filename in filenames:
                disk_path = os.path.join(root, filename)
                repo_path = posixpath.normpath(
                    posixpath.join(prefix, rel_root, filename)
                )
                self.index.add(
                    pygit2.IndexEntry(
                        repo_path,
                        self.repo.create_blob_fromdisk(disk_path),
                        os.stat(disk_path).st_mode,
                    )
                )

    def add_from_other_repo(self, other_repo, committish, src="", dest="", exclude=()):
        """Copies files from another repository over to this one."""

        def _copy_blob(blob_id):
            # Copy object over to this repo
            local_id = self.repo.create_blob(other_repo[blob_id].read_raw())
            # Two blobs with same content MUST have the same id in both repos
            assert local_id == blob_id

        commit = resolve_committish(other_repo, committish)
        node = commit.tree
        src = posixpath.normpath(src)
        dest = posixpath.normpath(dest)

        # Traverse down to the proper subtree or blob
        if src != ".":
            for part in src.split(os.sep):
                if not isinstance(node, pygit2.Tree):
                    # We reached a leaf and hence can't descend further
                    raise KeyError(part)
                node /= part

        if isinstance(node, pygit2.Blob):
            # Copy only a single file
            if dest == ".":
                # When nothing is specified as destination, keep the file's name
                dest = posixpath.split(src)[1]
            _copy_blob(node.id)
            self.index.add(pygit2.IndexEntry(dest, node.id, node.filemode))
            return

        assert isinstance(node, pygit2.Tree)
        # Read tree into in-memory index and then copy the entries over
        index = pygit2.Index()
        index.read_tree(node)
        for entry in index:
            # Check if file (or the directory the file is in) is in exclude list
            for path in exclude:
                if entry.path == path or entry.path.startswith(f"{path}/"):
                    break
            else:
                _copy_blob(entry.id)
                self.index.add(
                    pygit2.IndexEntry(
                        posixpath.normpath(posixpath.join(dest, entry.path)),
                        entry.id,
                        entry.mode,
                    )
                )

    def commit(self, sig, msg, to_refname=None):
        """Commit the index's state and return the commit object id."""
        tree_id = self.index.write_tree(self.repo)
        # Allow creating initial commits (with no parent)
        parents = [] if self.base_commit_id is None else [self.base_commit_id]
        commit_id = self.repo.create_commit(to_refname, sig, sig, msg, tree_id, parents)
        # This is now same as index and could be used as parent for subsequent commits
        self.base_commit_id = commit_id
        return commit_id

    def load_base(self, committish):
        """Load tree the committish points to into index.

        ``None`` will empty the index and cause the next commit to have no parent.
        """
        if committish is None:
            # Empty index, e.g. for creating the initial commit
            self.base_commit_id = None
            self.index.clear()
        else:
            base_commit = resolve_committish(self.repo, committish)
            # Read tree of the commit into index
            self.index.read_tree(base_commit.tree)
            self.base_commit_id = base_commit.id

    def remove(self, path):
        """Removes given path or, if it's a directory, everything under it.

        KeyError is raised when the given path didn't exist.
        """
        path = posixpath.normpath(path)
        if path == ".":
            self.index.remove_all()
            return
        dir_prefix = path + "/"
        to_remove = []
        for entry in self.index:
            if entry.path == path or entry.path.startswith(dir_prefix):
                to_remove.append(entry.path)
        if not to_remove:
            raise KeyError(path)
        for path in to_remove:
            self.index.remove(path)

    def write_to_fs(self, dest_dir, prefix=""):
        """Write directory structure from index to a directory.

        If prefix is specified, only that file/subdirectory will be extracted.
        """
        prefix = posixpath.normpath(prefix)
        for entry in self.index:
            # Links and commits can't be written
            if entry.mode not in (
                pygit2.GIT_FILEMODE_BLOB,
                pygit2.GIT_FILEMODE_BLOB_EXECUTABLE,
            ):
                continue
            if (
                prefix != "."
                and entry.path != prefix
                and not entry.path.startswith(prefix + "/")
            ):
                continue
            if prefix == entry.path:
                # Extract a single file
                rel_path = prefix
            else:
                rel_path = posixpath.relpath(entry.path, prefix)
            path = os.path.normpath(os.path.join(dest_dir, rel_path))
            try:
                os.makedirs(os.path.dirname(path))
            except FileExistsError:
                pass
            with open(path, "wb") as file:
                file.write(self.repo[entry.id].read_raw())
            os.chmod(path, entry.mode)
