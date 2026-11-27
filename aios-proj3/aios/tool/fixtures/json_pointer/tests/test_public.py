import unittest

from pointer_codec import escape, unescape
from pointer_path import join_pointer, split_pointer


class PointerTests(unittest.TestCase):
    def test_escape_sequence_is_decoded_once(self):
        self.assertEqual(unescape('~01'), '~1')

    def test_root_and_empty_key_are_distinct(self):
        self.assertEqual(join_pointer([]), '')
        self.assertEqual(split_pointer('/'), [''])

    def test_slash_and_tilde(self):
        self.assertEqual(escape('a/b~c'), 'a~1b~0c')
        self.assertEqual(split_pointer('/a~1b~0c'), ['a/b~c'])
