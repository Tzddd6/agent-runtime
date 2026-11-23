"""Best-effort console progress must not abort durable benchmark results."""
import builtins
import os
import sys


def status_print(*args, **kwargs):
    try:
        builtins.print(*args, **kwargs)
        return True
    except (BrokenPipeError, ConnectionResetError):
        # Also prevent CPython's final stdout flush from failing with exit120.
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
        return False
