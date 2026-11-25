"""Verify every delivery file; tolerate renamed Unicode files by content, never skip them."""
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath


def verify(root):
    root = Path(root)
    records = []
    names = set()
    for line in (root / 'FILES.sha256').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            raise ValueError('Invalid manifest record')
        digest, name = match.groups()
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or name in names:
            raise ValueError('Unsafe or duplicate manifest path: ' + name)
        names.add(name)
        records.append((digest, name))
    if not records:
        raise ValueError('Empty manifest rejected')
    actual = {}
    for p in root.rglob('*'):
        if p.is_symlink():
            raise ValueError('Symlink rejected: ' + str(p))
        if p.is_file() and p != root / 'FILES.sha256':
            actual[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    used = set()
    aliases = {}
    # Reserve exact names first, so a renamed file cannot borrow another manifest entry.
    for digest, name in records:
        if name in actual:
            if actual[name] != digest:
                raise ValueError('Checksum mismatch: ' + name)
            used.add(name)
    for digest, name in records:
        if name in actual:
            continue
        if name.isascii():
            raise ValueError('Missing ASCII file: ' + name)
        parent = PurePosixPath(name).parent
        matches = [n for n, h in actual.items() if n not in used and h == digest and PurePosixPath(n).parent == parent]
        if len(matches) != 1:
            raise ValueError('Missing/ambiguous Unicode file; re-extract with python -m zipfile: ' + name)
        aliases[name] = matches[0]
        used.add(matches[0])
    extra = set(actual) - used
    if extra:
        raise ValueError('Untracked files: ' + repr(sorted(extra)))
    return {'verified_files': len(records), 'filename_aliases': aliases, 'partial_verification': False}


if __name__ == '__main__':
    print(json.dumps(verify(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent), ensure_ascii=True, indent=2))
