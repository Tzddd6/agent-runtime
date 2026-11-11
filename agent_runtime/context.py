"""工作流内不可变对象存储；权限在 Runtime 边界检查，不是 OS 安全沙箱。"""
import hashlib
import gzip
import json
import re
import threading


class ContextStore:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.grants = {}
        self.lock = threading.RLock()
        self.metrics = {'puts': 0, 'unique_objects': 0, 'logical_bytes': 0, 'stored_bytes': 0}

    def put(self, data, readers):
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(raw) > 1024 * 1024:
            raise ValueError('单个上下文不得超过 1 MiB')
        ref = hashlib.sha256(raw).hexdigest()
        with self.lock:
            path = self.root / ref
            self.metrics['puts'] += 1
            self.metrics['logical_bytes'] += len(raw)
            if not path.exists():
                compressed = gzip.compress(raw, mtime=0) if len(raw) >= 1024 else raw
                encoded = compressed if len(compressed) < len(raw) else raw
                path.write_bytes(encoded)
                self.metrics['unique_objects'] += 1
                self.metrics['stored_bytes'] += len(encoded)
            self.grants.setdefault(ref, set()).update(readers)
        return ref

    def get(self, ref, agent):
        with self.lock:
            if (not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{64}', ref)
                    or agent not in self.grants.get(ref, set())):
                raise PermissionError('上下文未授权')
            raw = (self.root / ref).read_bytes()
            if raw.startswith(b'\x1f\x8b'):
                raw = gzip.decompress(raw)
            if hashlib.sha256(raw).hexdigest() != ref:
                raise ValueError('上下文完整性校验失败')
            return json.loads(raw)

    def stats(self):
        with self.lock:
            return dict(self.metrics)
