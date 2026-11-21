"""Default data paths use only isolated temporary marker files."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class DataLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.program = self.root / 'program'
        self.program.mkdir()
        self.portable = self.program / 'GrokDeskData'
        self.local_appdata = self.root / 'local-appdata'
        self.local = self.local_appdata / 'GrokDesk'
        patches = [
            mock.patch.object(app.sys, 'frozen', True, create=True),
            mock.patch.object(app.sys, 'executable', str(self.program / 'GrokDesk.exe')),
            mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.local_appdata)}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_existing_portable_marker_takes_priority(self):
        self.portable.mkdir()
        self.local.mkdir(parents=True)
        for marker in ('history.sqlite3', 'settings.json'):
            with self.subTest(marker=marker):
                path = self.portable / marker
                path.touch()
                with mock.patch.object(Path, 'read_text', side_effect=AssertionError('Do not read settings')):
                    self.assertEqual(app.default_data_directory(), self.portable)
                path.unlink()

    def test_without_portable_data_defaults_to_local_appdata(self):
        self.portable.mkdir()
        self.assertEqual(app.default_data_directory(), self.local)
        self.assertTrue(self.local.is_dir())
        self.assertEqual(list(self.local.iterdir()), [])

    def test_both_candidates_unwritable_raise_error(self):
        with mock.patch.object(Path, 'write_bytes', side_effect=PermissionError('Synthetic permission denial')) as write:
            with self.assertRaisesRegex(RuntimeError, '没有可写的数据目录'):
                app.default_data_directory()
        self.assertEqual(write.call_count, 2)

    def test_local_permission_failure_falls_back_to_portable(self):
        write_bytes = Path.write_bytes

        def write_probe(path, data):
            if path.parent == self.local:
                raise PermissionError('Synthetic local permission denial')
            return write_bytes(path, data)

        with mock.patch.object(Path, 'write_bytes', new=write_probe):
            self.assertEqual(app.default_data_directory(), self.portable)
        self.assertEqual(list(self.portable.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
