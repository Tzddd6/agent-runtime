import hashlib
import tempfile
import unittest
from pathlib import Path
from verify_delivery import verify


class DeliveryVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'docs').mkdir()
        self.files = {'core.py': b'core', 'docs/报告.md': b'report'}
        for name, data in self.files.items():
            (self.root / name).write_bytes(data)
        self.manifest()

    def manifest(self):
        (self.root / 'FILES.sha256').write_text(''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in self.files.items()), encoding='utf-8')

    def test_normal(self):
        self.assertEqual(verify(self.root)['verified_files'], 2)

    def test_renamed_unicode_still_checked(self):
        (self.root / 'docs/报告.md').rename(self.root / 'docs/garbled.md')
        self.assertFalse(verify(self.root)['partial_verification'])
        self.assertEqual(len(verify(self.root)['filename_aliases']), 1)

    def test_missing_unicode_rejected(self):
        (self.root / 'docs/报告.md').unlink()
        with self.assertRaises(ValueError): verify(self.root)

    def test_corrupt_unicode_rejected(self):
        (self.root / 'docs/报告.md').write_bytes(b'changed')
        with self.assertRaises(ValueError): verify(self.root)

    def test_missing_ascii_rejected(self):
        (self.root / 'core.py').rename(self.root / 'renamed.py')
        with self.assertRaises(ValueError): verify(self.root)

    def test_untracked_rejected(self):
        (self.root / 'extra').write_bytes(b'extra')
        with self.assertRaises(ValueError): verify(self.root)

    def test_duplicate_rejected(self):
        p = self.root / 'FILES.sha256'
        p.write_text(p.read_text(encoding='utf-8') * 2, encoding='utf-8')
        with self.assertRaises(ValueError): verify(self.root)

    def test_unsafe_rejected(self):
        (self.root / 'FILES.sha256').write_text('0'*64 + '  ../outside\n', encoding='utf-8')
        with self.assertRaises(ValueError): verify(self.root)

    def test_wrong_parent_rejected(self):
        (self.root / 'docs/报告.md').rename(self.root / 'garbled.md')
        with self.assertRaises(ValueError): verify(self.root)


if __name__ == '__main__': unittest.main()
