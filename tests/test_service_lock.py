import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuServiceLock import ServiceAlreadyRunning, ServiceLock


class ServiceLockTest(unittest.TestCase):

    def test_only_one_process_lock_holder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.lock"
            first = ServiceLock(path).acquire()
            try:
                with self.assertRaises(ServiceAlreadyRunning):
                    ServiceLock(path).acquire()
            finally:
                first.release()
            second = ServiceLock(path).acquire()
            self.assertTrue(second.acquired)
            second.release()

    def test_repeated_acquire_and_release_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = ServiceLock(Path(directory) / "service.lock")
            self.assertIs(lock, lock.acquire())
            self.assertIs(lock, lock.acquire())
            lock.release()
            lock.release()
            self.assertFalse(lock.acquired)


if __name__ == "__main__":
    unittest.main()
