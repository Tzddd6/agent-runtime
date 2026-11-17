"""工作流内不可变对象存储；权限在 Runtime 边界检查，不是 OS 安全沙箱。"""
import hashlib
import json
import re


class ContextStore:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.grants = {}

    def put(self, data, readers):
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ref = hashlib.sha256(raw).hexdigest()
        path = self.root / ref
        if not path.exists():
            path.write_bytes(raw)
        self.grants.setdefault(ref, set()).update(readers)
        return ref

    def get(self, ref, agent):
        if not re.fullmatch('[0-9a-f]{64}', ref) or agent not in self.grants.get(ref, set()):
            raise PermissionError('上下文未授权')
        return json.loads((self.root / ref).read_text(encoding='utf-8'))
