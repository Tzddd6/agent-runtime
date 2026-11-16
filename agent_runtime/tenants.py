"""Partition HTTP state by nginx-validated Basic Auth identity.

Only enable behind an authenticated reverse proxy that supplies the private
proxy token. nginx overwrites X-AgentRT-User with $remote_user, never JSON.
"""
import hashlib
import re
import threading
import json
import os


def authenticated_user(headers):
    value = headers.get('X-AgentRT-User', '')
    try:
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', value):raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise PermissionError('需要受保护入口提供有效登录身份') from None


class TenantRouter:
    def __init__(self, controller, legacy_user='remote'):
        self.base = controller; self.legacy_user = legacy_user
        self.lock = threading.RLock()
        self.controllers = {legacy_user: controller}
        controller.user_identity = legacy_user
        if controller.memory.user != legacy_user:
            from .memory_repository import MemoryRepository
            controller.memory=MemoryRepository(controller.root/'memory',legacy_user)
        controller.global_gate = self
        tenant_root=controller.root/'tenants'
        if tenant_root.exists():
            for folder in tenant_root.iterdir():
                if not re.fullmatch('[0-9a-f]{64}',folder.name):continue
                manifest=folder/'tenant.json'
                if folder.is_symlink() or manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size>512:
                    raise ValueError('用户分区索引损坏，拒绝启动')
                user=json.loads(manifest.read_text())['user']
                if not isinstance(user,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}',user) or hashlib.sha256(user.encode()).hexdigest()!=folder.name:
                    raise ValueError('用户分区身份校验失败')
                self.resolve(user)

    def resolve(self, user):
        with self.lock:
            if user not in self.controllers:
                if len(self.controllers) >= 16:raise ValueError('用户分区容量已满')
                from .web import Controller
                key = hashlib.sha256(user.encode()).hexdigest()
                self.controllers[user] = Controller(self.base.root/'tenants'/key,
                    self.base.cgroup_root, user_identity=user)
                path=self.controllers[user].root/'tenant.json'
                if not path.exists():
                    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'w') as stream:
                        json.dump({'user':user},stream);stream.flush();os.fsync(stream.fileno())
                self.controllers[user].global_gate = self
            return self.controllers[user]

    def check_idle(self):
        for c in self.controllers.values():
            if c.recovery_required:raise ValueError('存在待确认的资源回收，暂停所有用户准入')
            if any(j['status'] in ('running','cancelling') for j in c.jobs.values()):
                raise ValueError('已有工作流运行中，请等待结束或停止后再提交')
