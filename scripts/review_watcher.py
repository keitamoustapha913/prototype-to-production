"""Filesystem change detection for local review source trees."""
import ctypes
import os
from pathlib import Path
import select
import struct


class FileChanges:
    """Watch review input directories with inotify, falling back to stamp scans."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.fd = None
        self.libc = None
        self.watches = {}
        self.watched_files = {}
        self.input_directories = set()
        self.symlinks = set()
        self.rearm = False
        self.dirty = True
        self.watch_failed = False
        if os.name == 'posix' and hasattr(os, 'O_NONBLOCK'):
            try:
                self.libc = ctypes.CDLL(None, use_errno=True)
                self.libc.inotify_init1.argtypes = [ctypes.c_int]
                self.libc.inotify_init1.restype = ctypes.c_int
                self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                self.libc.inotify_add_watch.restype = ctypes.c_int
                self.fd = self.libc.inotify_init1(os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
                if self.fd < 0:
                    self.fd = None
            except (AttributeError, OSError):
                self.fd = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def add_watch(self, directory, mask):
        return self.libc.inotify_add_watch(self.fd, os.fsencode(directory), mask)

    def remove_watch(self, wd):
        self.libc.inotify_rm_watch(self.fd, wd)

    def refresh(self, paths):
        if self.fd is None or self.watch_failed:
            self.dirty = True
            return
        files = {}
        dirs = set()
        symlinks = set()
        for relative in paths:
            lexical = self.root / relative
            resolved = lexical.resolve()
            if lexical != resolved:
                symlinks.add(lexical)
            for target in (lexical, resolved):
                files.setdefault(target, set()).add(relative)
                parent = target.parent
                while parent not in dirs and parent.is_relative_to(self.root):
                    dirs.add(parent)
                    if parent == self.root:
                        break
                    parent = parent.parent
        if files != self.watched_files:
            # Reconcile edits made while the input inventory was changing.
            self.dirty = True
        self.watched_files = files
        self.input_directories = dirs
        self.symlinks = symlinks
        dirs = {directory.resolve() for directory in dirs if directory.is_dir()}
        if not self.rearm and dirs == set(self.watches.values()):
            return
        self.rearm = False
        for wd in self.watches:
            self.remove_watch(wd)
        self.watches.clear()
        mask = (0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 |
                0x00000100 | 0x00000200 | 0x00000400 | 0x00000800)
        for directory in dirs:
            wd = self.add_watch(directory, mask)
            if wd >= 0:
                self.watches[wd] = directory
            else:
                # A partial watch set can miss edits indefinitely. Use the
                # existing stamp scan on each request if any watch cannot be added.
                self.watch_failed = True
                for active in self.watches:
                    self.remove_watch(active)
                self.watches.clear()
                self.dirty = True
                return
        self.dirty = True

    def changed_paths(self):
        """Return observed source names, or None when a full reconciliation is necessary."""
        if self.fd is None or self.watch_failed:
            return None
        reconcile = self.dirty
        self.dirty = False
        changed = set()
        try:
            while select.select([self.fd], [], [], 0)[0]:
                events = os.read(self.fd, 65536)
                offset = 0
                while offset + 16 <= len(events):
                    wd, mask, _, length = struct.unpack_from('iIII', events, offset)
                    offset += 16
                    name = os.fsdecode(events[offset:offset + length].split(b'\0', 1)[0])
                    offset += length
                    directory = self.watches.get(wd)
                    if mask & 0x00008000:  # IN_IGNORED
                        self.watches.pop(wd, None)
                    if directory is None:
                        if mask & 0x00004000:  # IN_Q_OVERFLOW
                            self.rearm = reconcile = True
                        continue
                    if not name or mask & 0x00008000:
                        self.rearm = reconcile = True
                        continue
                    path = directory / name
                    if path in self.input_directories:
                        self.rearm = reconcile = True
                    if path in self.watched_files:
                        changed.update(self.watched_files[path])
                        # A renamed file can now resolve into a different watched directory.
                        if path in self.symlinks or path.is_symlink():
                            reconcile = True
        except BlockingIOError:
            pass
        return None if reconcile else changed

    def changed(self):
        paths = self.changed_paths()
        return paths is None or bool(paths)
