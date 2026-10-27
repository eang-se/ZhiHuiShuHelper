import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import mastery
from tools.package_release import package


class DistributionTests(unittest.TestCase):
    def test_frozen_app_data_root_is_next_to_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'ZhiHuiShuHelper.exe'
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
                self.assertEqual(mastery.app_root(), Path(tmp).resolve())

    def test_package_contains_only_built_app_and_public_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / 'app'
            (app / '_internal' / 'driver').mkdir(parents=True)
            (app / 'ZhiHuiShuHelper.exe').write_bytes(b'fake executable')
            (app / '_internal' / 'driver' / 'node.exe').write_bytes(b'fake driver')
            archive = package('v1.1.0', root / 'output', app)
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn('ZhiHuiShuHelper/ZhiHuiShuHelper.exe', names)
                self.assertIn('ZhiHuiShuHelper/profile.polymas.json', names)
                self.assertIn('ZhiHuiShuHelper/_internal/driver/node.exe', names)
                self.assertIsNone(bundle.testzip())
            for name in ('data/question-bank.sqlite3', 'diagnostics/page.html', '_internal/.local-browsers/chromium/chrome.exe'):
                path = app / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'should not be included')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    package('v1.1.0', root / 'output', app)
                path.unlink()

    def test_invalid_version_and_missing_executable_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                package('../invalid', root, root)
            with self.assertRaises(FileNotFoundError):
                package('v1.1.0', root, root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
