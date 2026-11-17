"""工作流内不可变对象存储；权限在 Runtime 边界检查，不是 OS 安全沙箱。"""
import hashlib
import gzip
import json
import re
import threading
import os


class ContextStore:
    def __init__(self, root, *, cache_bytes=8*1024**2, cache_policy='lru2', cache_max_entries=1024,compression_level=9,durable=False):
        if type(durable) is not bool:raise ValueError('durable must be a boolean')
        self.durable=durable
        if type(compression_level) is not int or not 0<=compression_level<=9:raise ValueError('compression_level必须为0—9')
        self.compression_level=compression_level
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError('cache_bytes 必须为非负整数')
        if cache_policy not in ('lru','lru2'):
            raise ValueError('缓存策略必须为 lru 或 lru2')
        if type(cache_max_entries) is not int or cache_max_entries < 1:
            raise ValueError('cache_max_entries 必须为正整数')
        self.root = root
        if self.root.is_symlink():raise ValueError('上下文目录不能是符号链接')
        self.root.mkdir(parents=True, exist_ok=True,mode=0o700)
        if os.name=='posix':self.root.chmod(0o700)
        self.grants = {}
        self.sources = {}
        self.lock = threading.RLock()
        self.metrics = {'puts': 0, 'unique_objects': 0, 'logical_bytes': 0, 'stored_bytes': 0}
        # Cache raw immutable JSON, never the caller's mutable Python objects.
        self.cache_limit = cache_bytes
        self.cache_policy = cache_policy
        self.cache_max_entries = cache_max_entries
        self.cache = {}
        self.clock = 0
        self.metrics.update(cache_hits=0,cache_misses=0,cache_evictions=0,
                            cache_invalidations=0,cache_bypasses=0,cache_bytes=0,
                            disk_reads=0,disk_read_bytes=0)

    def _drop(self, ref, reason):
        entry = self.cache.pop(ref, None)
        if entry is not None:
            self.metrics['cache_bytes'] -= len(entry['raw'])
            self.metrics[reason] += 1

    def _remember(self, ref, raw, signature):
        if not self.cache_limit or len(raw) > self.cache_limit:
            self.metrics['cache_bypasses'] += 1
            return
        while (self.metrics['cache_bytes']+len(raw)>self.cache_limit
               or len(self.cache)>=self.cache_max_entries):
            def priority(key):
                history = self.cache[key]['history']
                if self.cache_policy == 'lru':
                    return (0,history[-1])
                # LRU-2: one-hit scan items leave before repeatedly used items.
                return (len(history)>=2,history[0])
            victim = min(self.cache,key=priority)
            self._drop(victim,'cache_evictions')
        self.cache[ref] = {'raw':raw,'signature':signature,'history':[self.clock]}
        self.metrics['cache_bytes'] += len(raw)

    def put(self, data, readers):
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(raw) > 1024 * 1024:
            raise ValueError('单个上下文不得超过 1 MiB')
        ref = hashlib.sha256(raw).hexdigest()
        with self.lock:
            path = self.root / ref
            if path.is_symlink():raise ValueError('上下文对象不能是符号链接')
            self.metrics['puts'] += 1
            self.metrics['logical_bytes'] += len(raw)
            if not path.exists():
                # Prefer bounded CPU cost for short-lived runtime objects. Readers
                # still verify the same uncompressed SHA; compression is storage only.
                compressed = gzip.compress(raw, compresslevel=self.compression_level, mtime=0) if len(raw) >= 1024 else raw
                encoded = compressed if len(compressed) < len(raw) else raw
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
                with os.fdopen(fd,'wb') as file:
                    file.write(encoded)
                    if self.durable:file.flush();os.fsync(file.fileno())
                if self.durable:
                    descriptor=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(descriptor)
                    finally:os.close(descriptor)
                self.metrics['unique_objects'] += 1
                self.metrics['stored_bytes'] += len(encoded)
            self.grants.setdefault(ref, set()).update(readers)
        return ref

    def get(self, ref, agent):
        with self.lock:
            if (not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{64}', ref)
                    or agent not in self.grants.get(ref, set())):
                raise PermissionError('上下文未授权')
            path = self.root / ref
            if path.is_symlink():raise ValueError('上下文对象不能是符号链接')
            info = path.stat()
            if info.st_size>1024*1024:
                self._drop(ref,'cache_invalidations')
                raise ValueError('上下文文件超过 1 MiB')
            signature = (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            entry = self.cache.get(ref)
            self.clock += 1
            if entry is not None and entry['signature'] != signature:
                self._drop(ref,'cache_invalidations')
                entry = None
            if entry is not None:
                raw = entry['raw']
                self.metrics['cache_hits'] += 1
                entry['history'] = (entry['history']+[self.clock])[-2:]
            else:
                self.metrics['cache_misses'] += 1
                # The stat check is advisory: bound the read as well in case a
                # same-user writer grows/replaces the file between stat/open.
                fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                with os.fdopen(fd,'rb') as source:
                    raw = source.read(1024*1024+1)
                self.metrics['disk_reads'] += 1
                self.metrics['disk_read_bytes'] += len(raw)
                if len(raw)>1024*1024:
                    raise ValueError('上下文文件超过 1 MiB')
                if raw.startswith(b'\x1f\x8b'):
                    # Bound decoding even if a same-user process corrupts the file.
                    import io
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as encoded:
                        raw = encoded.read(1024*1024+1)
                if len(raw)>1024*1024:
                    raise ValueError('上下文解压后超过 1 MiB')
            if hashlib.sha256(raw).hexdigest() != ref:
                self._drop(ref,'cache_invalidations')
                raise ValueError('上下文完整性校验失败')
            if entry is None:
                self._remember(ref,raw,signature)
            return json.loads(raw)

    def stats(self):
        with self.lock:
            return {**self.metrics,'cache_entries':len(self.cache),
                    'cache_limit_bytes':self.cache_limit,'cache_limit_entries':self.cache_max_entries,
                    'cache_policy':self.cache_policy}

    def grant(self, ref, owner, reader):
        """Coordinator-only sharing; a digest alone never grants access."""
        with self.lock:
            if owner not in self.grants.get(ref,set()):
                raise PermissionError('上下文未授权')
            self.grants[ref].add(reader)

    def authorized(self,agent):
        with self.lock:return [ref for ref,readers in self.grants.items() if agent in readers]

    def get_many(self,refs,agent,max_bytes=None):
        if not isinstance(refs,list) or len(refs)>164:raise ValueError('引用批次数超过限制')
        with self.lock:
            if any(not isinstance(ref,str) or not re.fullmatch('[0-9a-f]{64}',ref) or agent not in self.grants.get(ref,set()) for ref in refs):
                raise PermissionError('上下文未授权')
            result={};size=0
            for ref in dict.fromkeys(refs):
                value=self.get(ref,agent);size+=len(json.dumps(value,ensure_ascii=False).encode())
                if max_bytes is not None and size>max_bytes:raise ValueError('上下文批次超过字节上限')
                result[ref]=value
            return result

    def label_source(self,ref,metadata):
        if (ref not in self.grants or not isinstance(metadata,dict) or set(metadata)-{'task_id','agent','action','attempt','kind'}
                or len(json.dumps(metadata))>1024):raise ValueError('来源描述无效')
        with self.lock:
            entries=self.sources.setdefault(ref,[])
            if metadata not in entries:entries.append(dict(metadata));del entries[:-16]

    def source_info(self,ref):
        with self.lock:return [dict(row) for row in self.sources.get(ref,[])]

    def revoke_agent(self,agent):
        with self.lock:
            for readers in self.grants.values():readers.discard(agent)
