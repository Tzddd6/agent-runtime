import io
import sys
import unittest
from unittest.mock import patch
from aios.benchmarks.progress import status_print


class BenchmarkProgressTests(unittest.TestCase):
    def test_closed_observation_pipe_does_not_abort_benchmark(self):
        class ClosedPipe(io.StringIO):
            def write(self, value):
                raise BrokenPipeError('synthetic closed observer')
        with patch('sys.stdout', ClosedPipe()):
            self.assertFalse(status_print('DONE', flush=True))
            sink = sys.stdout
            try:
                self.assertTrue(status_print('continuing', flush=True))
            finally:
                sink.close()
