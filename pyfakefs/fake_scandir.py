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
        self._inode = None
        self._islink = False
        self._isdir = False
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
                ``follow_symlinks`` is set, and this entry points to an existing
                directory entry.
        """
        if follow_symlinks and self._islink:
            # the type of the link target is not part of the entry snapshot
            try:
                return S_ISDIR(self.stat().st_mode)
            except (FileNotFoundError, NotADirectoryError):
                # the real implementation ignores these errors
                return False
        # for non-links, the entry type has been captured in the snapshot
        return self._isdir

    def is_file(self, follow_symlinks=True):
        """Return `True` if this entry is a regular file entry.

        Args:
            follow_symlinks: If `True`, also return `True` if this entry is a
                symlink pointing to a regular file.

        Returns:
            `True` if this entry is an existing file entry, or if
                ``follow_symlinks`` is set, and this entry points to an existing
                file entry.
        """
        if follow_symlinks and self._islink:
            # the type of the link target is not part of the entry snapshot
            try:
                return S_ISREG(self.stat().st_mode)
            except (FileNotFoundError, NotADirectoryError):
                # the real implementation ignores these errors
                return False
        # for non-links, the entry type has been captured in the snapshot
        return not self._isdir and not self._islink

    def is_symlink(self):
        """Return `True` if this entry is a symbolic link (even if broken)."""
        return self._islink

    def stat(self, follow_symlinks=True):
        """Return a `stat_result` object for this entry.

        Args:
            follow_symlinks: If `False` and the entry is a symlink, return the
                result for the symlink, otherwise for the object it points to.

        The result is cached per `follow_symlinks` value, as in the real
        implementation. If the entry no longer exists and the result has not
        been cached, `FileNotFoundError` is raised.
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

    Takes a snapshot of the directory entries at creation, so that entries
    added to or removed from the directory during the iteration do not
    affect it. The snapshot is released when the iterator is exhausted or
    closed.
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
        entries = self.filesystem.confirmdir(self.abspath, check_exe_perm=False).entries
        # snapshot the information the real implementation gets from
        # `readdir`: the entry names, their types and their inode numbers
        self._entries = [
            (name, S_ISDIR(entry.st_mode), S_ISLNK(entry.st_mode), entry.st_ino)
            for name, entry in entries.items()
        ]
        self._pos = 0

    @property
    def filesystem(self) -> FakeFilesystem:
        fs = self._filesystem()
        assert fs is not None
        return fs

    def __iter__(self):
        return self

    def __next__(self):
        if self._entries is None:
            raise StopIteration
        if self._pos >= len(self._entries):
            self.close()
            raise StopIteration
        name, is_dir, is_link, inode = self._entries[self._pos]
        self._pos += 1
        dir_entry = DirEntry(self.filesystem)
        dir_entry.name = name
        dir_entry.path = self.filesystem.joinpaths(self.path, dir_entry.name)
        dir_entry._abspath = self.filesystem.joinpaths(self.abspath, dir_entry.name)
        dir_entry._isdir = is_dir
        dir_entry._islink = is_link
        dir_entry._inode = inode
        return dir_entry

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Release the snapshot of the directory entries."""
        self._entries = None


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


def walk(filesystem, top, topdown=True, onerror=None, followlinks=False):
    """Perform an os.walk operation over the fake filesystem.

    Args:
        filesystem: The fake filesystem used for implementation
        top: The root directory from which to begin walk.
        topdown: Determines whether to return the tuples with the root as
            the first entry (`True`) or as the last, after all the child
            directory tuples (`False`).
      onerror: If not `None`, function which will be called to handle the
            `os.error` instance provided when `os.listdir()` fails.
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
            scandir_it = scandir(filesystem, top_dir)
        except OSError as exc:
            if onerror is not None:
                onerror(exc)
            return

        dirs = []
        nondirs = []
        with scandir_it:
            for entry in scandir_it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    # if the entry cannot be stated, it is treated as
                    # a non-directory, as in the real implementation
                    is_dir = False
                if is_dir:
                    dirs.append(entry.name)
                else:
                    nondirs.append(entry.name)
        top_contents = (top_dir, dirs, nondirs)

        if topdown:
            yield top_contents

        for directory in dirs:
            path = filesystem.joinpaths(top_dir, directory)
            if not followlinks and filesystem.islink(path):
                continue
            yield from do_walk(path)
        if not topdown:
            yield top_contents

    return do_walk(make_string_path(to_string(top)), top_most=True)
