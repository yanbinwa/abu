from __future__ import absolute_import

import fcntl
import os
from pathlib import Path


class ServiceAlreadyRunning(RuntimeError):
    pass


class ServiceLock(object):
    """Process-lifetime exclusive lock for the single-host v1 service."""

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    @property
    def acquired(self):
        return self._handle is not None

    def acquire(self):
        if self.acquired:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            handle.close()
            raise ServiceAlreadyRunning(
                "paper service lock is already held: {}".format(self.path)) from error
        handle.seek(0)
        handle.truncate()
        handle.write("{}\n".format(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self):
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
