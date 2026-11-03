import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run import _acquire_single_instance


class _Cfg:
    def __init__(self, db):
        self.db_path = db


class SingleInstanceLockTest(unittest.TestCase):
    def test_second_instance_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "scan.db")
            first = _acquire_single_instance(_Cfg(db))
            self.assertIsNotNone(first)
            second = _acquire_single_instance(_Cfg(db))
            self.assertIsNone(second)
            first.close()

    def test_released_on_close(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "scan.db")
            fh = _acquire_single_instance(_Cfg(db))
            fh.close()
            again = _acquire_single_instance(_Cfg(db))
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
