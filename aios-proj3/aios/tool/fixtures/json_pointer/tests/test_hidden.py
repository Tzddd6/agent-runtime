import unittest

from pointer_path import join_pointer, split_pointer


class PointerEdgeCases(unittest.TestCase):
    def test_adjacent_empty_tokens_round_trip(self):
        parts = ['', '雪', '', 'a/b', '~1', '']
        self.assertEqual(split_pointer(join_pointer(parts)), parts)

    def test_encoded_sequence_round_trip(self):
        self.assertEqual(split_pointer('/~00/~01/~10/~11'), ['~0', '~1', '/0', '/1'])

    def test_missing_initial_slash(self):
        with self.assertRaises(ValueError):
            split_pointer('first/second')
