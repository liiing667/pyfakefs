# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A fake implementation for the `scandir` function working with
FakeFilesystem.
"""

from __future__ import annotations

import os
import sys
import weakref
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import TYPE_CHECKING

from pyfakefs.helpers import IS_PYPY, make_string_path, to_string

if TYPE_CHECKING:
    from pyfakefs.fake_filesystem import FakeFilesystem

# Sentinel used by `pathlib.Path.walk()` in Python 3.13+ to signal that
# symbolic links to directories shall be treated as files. It is forwarded
# to `os.walk`, where it takes the place of `followlinks`.
_WALK_SYMLINKS_AS_FILES = getattr(os, "_walk_symlinks_as_files", None)


class DirEntry(os.PathLike):
    """Emulates os.DirEntry. Note that we did not enforce keyword only
    arguments."""

    def __init__(self, filesystem):
        """Initialize the dir entry with unset values.

        Args:
            filesystem: the fake filesystem used for implementation.
        """
        self._filesystem = weakref.ref(filesystem)
        self.name = ""
        self.path = ""
        self._abspath = ""
        # mode and inode of the entry itself, as read from the directory
        # snapshot (equivalent to the d_type and d_ino values that the real
        # scandir implementation caches from readdir)
        self._mode = None
        self._inode = None
        # cached results for symlinks that need a live lookup of the target
        self._isdir_link = None
        self._isfile_link = None
        self._statresult = None
        self._statresult_symlink = None

    @property
    def filesystem(self) -> FakeFilesystem:
        fs = self._filesystem()
        assert fs is not None
        return fs

    def inode(self):
        """Return the inode number of the entry."""
        if self._inode is None:
            self.stat(follow_symlinks=False)
        return self._inode

    def is_dir(self, follow_symlinks=True):
        """Return `True` if this entry is a directory entry.

        Args:
            follow_symlinks: If `True`, also return `True` if this entry is a
                symlink pointing to a directory.

        Returns:
            `True` if this entry is an existing directory entry, or if
                ``follow_symlinks`` is set, and this entry points to an
                existing directory entry.
        """
        if follow_symlinks and self.is_symlink():
            if self._isdir_link is None:
                self._isdir_link = self.filesystem.isdir(self._abspath)
            return self._isdir_link
        return bool(self._mode is not None and S_ISDIR(self._mode))

    def is_file(self, follow_symlinks=True):
        """Return `True` if this entry is a regular file entry.

        Args:
            follow_symlinks: If `True`, also return `True` if this entry is a
                symlink pointing to a regular file.

        Returns:
            `True` if this entry is an existing file entry, or if
                ``follow_symlinks`` is set, and this entry points to an
                existing file entry.
        """
        if follow_symlinks and self.is_symlink():
            if self._isfile_link is None:
                self._isfile_link = self.filesystem.isfile(self._abspath)
            return self._isfile_link
        return bool(self._mode is not None and S_ISREG(self._mode))

    def is_symlink(self):
        """Return `True` if this entry is a symbolic link (even if broken)."""
        return bool(self._mode is not None and S_ISLNK(self._mode))

    def stat(self, follow_symlinks=True):
        """Return a `stat_result` object for this entry.

        The result is cached like in the real `os.DirEntry`; repeated calls
        return the cached result even if the entry has been removed in the
        meantime, while the first call on a removed entry raises the same
        error as a stat call on the file system.

        Args:
            follow_symlinks: If `False` and the entry is a symlink, return the
                result for the symlink, otherwise for the object it points to.
        """
        if follow_symlinks:
            if self._statresult_symlink is None:
                file_object = self.filesystem.resolve(self._abspath)
                self._statresult_symlink = file_object.stat_result.copy()
                if self.filesystem.is_windows_fs and not IS_PYPY:
                    self._statresult_symlink.st_nlink = 0
            return self._statresult_symlink

        if self._statresult is None:
            file_object = self.filesystem.lresolve(self._abspath)
            self._inode = file_object.st_ino
            self._statresult = file_object.stat_result.copy()
            if self.filesystem.is_windows_fs:
                self._statresult.st_nlink = 0
        return self._statresult

    def __fspath__(self):
        return self.path

    if sys.version_info >= (3, 12):

        def is_junction(self) -> bool:
            """Return `True` if this entry is a junction.
            Junctions are not a part of posix semantic."""
            if not self.filesystem.is_windows_fs:
                return False
            file_object = self.filesystem.resolve(self._abspath)
            return file_object.is_junction


class ScanDirIter:
    """Iterator for DirEntry objects returned from `scandir()`
    function.

    The directory is opened and validated when the iterator is created, but
    its contents are snapshotted lazily when the iteration starts. Entries
    added or removed after that point do not affect this iteration, and the
    snapshot is released as soon as the iterator gets closed or exhausted.
    """

    def __init__(self, filesystem, path):
        self._filesystem = weakref.ref(filesystem)
        if isinstance(path, int):
            if self.filesystem.is_windows_fs:
                raise NotImplementedError(
                    "scandir does not support file descriptor path argument"
                )
            self.abspath = self.filesystem.absnormpath(
                self.filesystem.get_open_file(path)
                .get_object()
                .path  # pytype:disable=attribute-error
            )
            self.path = ""
        else:
            if path is None:
                path = "."
            path = make_string_path(path)
            self.abspath = self.filesystem.absnormpath(path)
            self.path = to_string(path)
        # like the real scandir, existence and access permissions are
        # checked when the iterator is created
        self._directory = self.filesystem.confirmdir(self.abspath, check_exe_perm=False)
        # the entry snapshot is taken lazily at the first iteration step;
        # it is a list of (name, mode, inode) tuples
        self._entries = None
        self._position = 0

    @property
    def filesystem(self) -> FakeFilesystem:
        fs = self._filesystem()
        assert fs is not None
        return fs

    def __iter__(self):
        return self

    def _take_snapshot(self):
        # tuple() takes the snapshot from the live directory object;
        # later additions or removals do not affect it
        return [
            (entry_name, entry_object.st_mode, entry_object.st_ino)
            for entry_name, entry_object in tuple(self._directory.entries.items())
        ]

    def __next__(self):
        if self._directory is None:
            # the iterator has been closed
            raise StopIteration
        if self._entries is None:
            self._entries = self._take_snapshot()
            self._position = 0
        if self._position >= len(self._entries):
            self.close()
            raise StopIteration
        name, mode, inode = self._entries[self._position]
        self._position += 1
        dir_entry = DirEntry(self.filesystem)
        dir_entry.name = to_string(name)
        dir_entry.path = self.filesystem.joinpaths(self.path, dir_entry.name)
        dir_entry._abspath = self.filesystem.joinpaths(self.abspath, dir_entry.name)
        dir_entry._mode = mode
        dir_entry._inode = inode
        return dir_entry

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        # release the snapshot and the directory reference, so that
        # long-running processes do not accumulate directory data
        self._entries = None
        self._directory = None


def scandir(filesystem, path=""):
    """Return an iterator of DirEntry objects corresponding to the entries
    in the directory given by path.

    Args:
        filesystem: The fake filesystem used for implementation
        path: Path to the target directory within the fake filesystem.

    Returns:
        an iterator to an unsorted list of os.DirEntry objects for
        each entry in path.

    Raises:
        OSError: if the target is not a directory.
    """
    return ScanDirIter(filesystem, path)


def _classify_directory_contents(filesystem, root, followlinks=False):
    """Classify contents of a directory as files/directories.

    The contents are read through a single `scandir` snapshot, so entries
    changed during the classification do not affect the result.

    Args:
        filesystem: the fake filesystem used for implementation.
        root: (str) Directory to examine.
        followlinks: If the special `os._walk_symlinks_as_files` sentinel,
            symlinks are never followed and symlinked directories are
            classified as files (used by `pathlib.Path.walk()`).

    Returns:
        (tuple) A tuple consisting of three values: the directory examined,
        a list containing all of the directory entries, and a list
        containing all of the non-directory entries.
        (This is the same format as returned by the `os.walk` generator.)

    Raises:
        Nothing on its own, but be ready to catch exceptions generated by
        underlying mechanisms like `scandir`.
    """
    dirs = []
    files = []
    with scandir(filesystem, root) as entries:
        for entry in entries:
            if followlinks is _WALK_SYMLINKS_AS_FILES:
                is_dir = entry.is_dir(follow_symlinks=False)
            else:
                is_dir = entry.is_dir()
            if is_dir:
                dirs.append(entry.name)
            else:
                files.append(entry.name)
    return root, dirs, files


def walk(filesystem, top, topdown=True, onerror=None, followlinks=False):
    """Perform an os.walk operation over the fake filesystem.

    Args:
        filesystem: The fake filesystem used for implementation
        top: The root directory from which to begin walk.
        topdown: Determines whether to return the tuples with the root as
            the first entry (`True`) or as the last, after all the child
            directory tuples (`False`).
      onerror: If not `None`, function which will be called to handle the
            `os.error` instance provided when `scandir()` fails.
      followlinks: If `True`, symbolic links are followed.

    Yields:
        (path, directories, nondirectories) for top and each of its
        subdirectories.  See the documentation for the builtin os module
        for further details.
    """

    def do_walk(top_dir, top_most=False):
        if not top_most and not followlinks and filesystem.islink(top_dir):
            return
        try:
            top_contents = _classify_directory_contents(
                filesystem, top_dir, followlinks
            )
        except OSError as exc:
            top_contents = None
            if onerror is not None:
                onerror(exc)

        if top_contents is not None:
            if topdown:
                yield top_contents

            for directory in top_contents[1]:
                path = filesystem.joinpaths(top_dir, directory)
                if not followlinks and filesystem.islink(path):
                    continue
                yield from do_walk(path)
            if not topdown:
                yield top_contents

    return do_walk(make_string_path(to_string(top)), top_most=True)
