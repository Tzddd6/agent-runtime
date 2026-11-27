from collections import OrderedDict
from contextlib import closing, contextmanager
import copy
import re
import sqlite3
import gzip
import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

_ORIG_SQLITE_CONNECT = sqlite3.connect


class ContextStore:
    """
    AIOS Kernel High-Efficiency Context Store (Proj3 Requirement 4.5).
    Implements:
    1. Context Reuse (上下文复用): SHA256 content-addressed deduplication & LRU-2 memory cache.
    2. Context Compression (上下文压缩): Lossless gzip disk compression + lossy byte-budget views & pagination.
    3. Context Isolation (上下文隔离): Per-object owner & Agent ACL table with explicit dynamic delegation.
    """

    def __init__(self, storage_dir: str, gzip_threshold_bytes: int = 256, cache_capacity: int = 64, persistent_connection: bool = False):
        self.storage_dir = os.path.abspath(storage_dir)
        os.makedirs(self.storage_dir, exist_ok=True)
        self.gzip_threshold_bytes = gzip_threshold_bytes
        self.cache_capacity = cache_capacity
        self._persistent_connection = persistent_connection
        self._db: Optional[sqlite3.Connection] = None

        self._lock = threading.RLock()
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._acls: Dict[str, Set[str]] = {}
        self._holders: Dict[str, Set[str]] = {}
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._access_counts: Dict[str, int] = {}

        # Metrics for experimental evaluation
        self.logical_bytes_written: int = 0
        self.physical_bytes_stored: int = 0
        self.unique_objects: int = 0
        self.dedup_hits: int = 0
        self.cache_hits: int = 0
        self._load_index()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    def _open_db(self):
        if self._persistent_connection and sqlite3.connect is _ORIG_SQLITE_CONNECT:
            if self._db is None:
                self._db = sqlite3.connect(self._index_path, timeout=5, check_same_thread=False)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=FULL")
            return self._db, False
        db = sqlite3.connect(self._index_path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db, True

    @contextmanager
    def _write_tx(self):
        db, should_close = self._open_db()
        try:
            yield db
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                if not should_close and self._db is db:
                    try:
                        db.close()
                    except Exception:
                        pass
                    self._db = None
            raise
        finally:
            if should_close:
                db.close()

    def _load_index(self):
        self._index_path = os.path.join(self.storage_dir, 'context-index.sqlite3')
        db, should_close = self._open_db()
        try:
            if should_close:
                db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, acl TEXT NOT NULL)')
            rows = db.execute('SELECT id, metadata, acl FROM objects').fetchall()
        finally:
            if should_close:
                db.close()
        for identity, metadata, acl in rows:
            meta = json.loads(metadata)
            if not re.fullmatch(r'ctx_[a-f0-9]{16,64}', identity) or not re.fullmatch(r'[a-f0-9]{64}', meta['sha256']):
                raise ValueError('Invalid context index')
            path = os.path.join(self.storage_dir, identity + ('.json.gz' if meta['compressed'] else '.json'))
            if os.path.islink(path) or not os.path.isfile(path):
                raise ValueError('Context index references an unavailable object')
            meta['file_path'] = path
            self._meta[identity] = meta
            self._acls[identity] = set(json.loads(acl))
            self._holders[identity] = set(meta.get('holders') or ([meta['owner_agent']] if meta.get('owner_agent') else []))
        self.unique_objects = len(self._meta)
        self.physical_bytes_stored = sum(meta['stored_bytes'] for meta in self._meta.values())
        self.logical_bytes_written = sum(meta['raw_bytes'] for meta in self._meta.values())

    def _persist_index(self, identity):
        with self._write_tx() as db:
            db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?)',
                       (identity, json.dumps(self._meta[identity]), json.dumps(sorted(self._acls[identity]))))

    @staticmethod
    def _sanitize_for_json(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: ContextStore._sanitize_for_json(v) for k, v in obj.items() if not callable(v)}
        if isinstance(obj, (list, tuple)):
            return [ContextStore._sanitize_for_json(x) for x in obj if not callable(x)]
        if callable(obj):
            return f"<callable:{getattr(obj, '__name__', 'fn')}>"
        return obj

    @staticmethod
    def _canonical_bytes(data: Any) -> bytes:
        clean = ContextStore._sanitize_for_json(data)
        return json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")

    def put(
        self,
        owner_agent: str,
        data: Any,
        authorized_agents: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        *,
        holder_agent: Optional[str] = None,
    ) -> str:
        holder = holder_agent or owner_agent
        raw_bytes = self._canonical_bytes(data)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        ctx_id = f"ctx_{digest}"
        raw_len = len(raw_bytes)
        if raw_len > 16 * 1024 * 1024:
            raise ValueError("Context object exceeds limit")

        with self._lock:
            old_logical = self.logical_bytes_written
            old_phys = self.physical_bytes_stored
            old_unique = self.unique_objects
            old_dedup = self.dedup_hits

            if ctx_id in self._meta:
                old_meta = dict(self._meta[ctx_id])
                old_acl = set(self._acls.get(ctx_id, set()))
                old_holders = set(self._holders.get(ctx_id, set()))
                new_acl = set(old_acl) | {owner_agent} | set(authorized_agents or ())
                new_holders = set(old_holders) | {holder}
                new_meta = {**old_meta, "holders": sorted(new_holders)}

                self._meta[ctx_id] = new_meta
                self._acls[ctx_id] = new_acl
                self._holders[ctx_id] = new_holders
                try:
                    self._persist_index(ctx_id)
                except Exception:
                    self._meta[ctx_id] = old_meta
                    self._acls[ctx_id] = old_acl
                    self._holders[ctx_id] = old_holders
                    raise
                self.logical_bytes_written += raw_len
                self.dedup_hits += 1
                self._touch_cache(ctx_id, json.loads(raw_bytes))
                return ctx_id

            # Compress with gzip if payload exceeds threshold and compression saves space
            compressed = False
            disk_bytes = raw_bytes
            if raw_len >= self.gzip_threshold_bytes:
                gz_bytes = gzip.compress(raw_bytes, compresslevel=6)
                if len(gz_bytes) < raw_len:
                    disk_bytes = gz_bytes
                    compressed = True

            ext = ".json.gz" if compressed else ".json"
            file_path = os.path.join(self.storage_dir, f"{ctx_id}{ext}")
            temporary = file_path + '.tmp'
            with open(temporary, 'wb') as f:
                f.write(disk_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, file_path)

            phys_len = len(disk_bytes)
            new_acl = {owner_agent} | set(authorized_agents or ())
            new_holders = {holder}
            new_meta = {
                "ctx_id": ctx_id,
                "sha256": digest,
                "owner_agent": owner_agent,
                "holders": sorted(new_holders),
                "raw_bytes": raw_len,
                "stored_bytes": phys_len,
                "compressed": compressed,
                "file_path": file_path,
                "tags": list(tags or []),
                "created_at": time.time(),
            }
            self._meta[ctx_id] = new_meta
            self._acls[ctx_id] = new_acl
            self._holders[ctx_id] = new_holders
            try:
                self._persist_index(ctx_id)
            except Exception:
                self._meta.pop(ctx_id, None)
                self._acls.pop(ctx_id, None)
                self._holders.pop(ctx_id, None)
                self.logical_bytes_written = old_logical
                self.physical_bytes_stored = old_phys
                self.unique_objects = old_unique
                self.dedup_hits = old_dedup
                try:
                    if os.path.exists(file_path):
                        os.unlink(file_path)
                except OSError:
                    pass
                raise
            self.logical_bytes_written += raw_len
            self.physical_bytes_stored += phys_len
            self.unique_objects += 1
            self._touch_cache(ctx_id, json.loads(raw_bytes))
            return ctx_id

    def _touch_cache(self, ctx_id: str, data: Any) -> None:
        self._access_counts[ctx_id] = self._access_counts.get(ctx_id, 0) + 1
        if ctx_id in self._cache:
            self._cache.move_to_end(ctx_id)
            return
        if len(self._cache) >= self.cache_capacity:
            # Evict lowest access count (LRU-2 style approximation)
            victim = min(self._cache.keys(), key=lambda k: (min(self._access_counts.get(k, 1), 2)))
            self._cache.pop(victim, None)
        self._cache[ctx_id] = data

    def _check_access(self, ctx_id: str, reader_agent: str) -> Dict[str, Any]:
        meta = self._meta.get(ctx_id)
        if meta is None:
            raise KeyError(f"Context object '{ctx_id}' not found")
        allowed = self._acls.get(ctx_id, set())
        if reader_agent not in allowed:
            raise PermissionError(
                f"Context ACL isolation blocked agent '{reader_agent}' from reading '{ctx_id}'"
            )
        return meta

    def grant_access(self, ctx_id: str, grantor_agent: str, target_agent: str) -> None:
        """Explicitly delegate read permission on `ctx_id` from an authorized grantor to `target_agent`."""
        with self._lock:
            self._check_access(ctx_id, grantor_agent)
            self._acls[ctx_id].add(target_agent)
            self._persist_index(ctx_id)

    def get(self, ctx_id: str, reader_agent: str) -> Any:
        with self._lock:
            meta = self._check_access(ctx_id, reader_agent)
            if ctx_id in self._cache:
                self.cache_hits += 1
                self._cache.move_to_end(ctx_id)
                return copy.deepcopy(self._cache[ctx_id])

            with open(meta["file_path"], "rb") as f:
                disk_bytes = f.read()
            raw_bytes = gzip.decompress(disk_bytes) if meta["compressed"] else disk_bytes
            if hashlib.sha256(raw_bytes).hexdigest() != meta["sha256"]:
                raise ValueError(f"Integrity check failed for context '{ctx_id}'")
            data = json.loads(raw_bytes.decode("utf-8"))
            self._touch_cache(ctx_id, data)
            return copy.deepcopy(data)

    def build_budget_view(
        self,
        ctx_id: str,
        reader_agent: str,
        byte_budget: int = 512,
    ) -> Dict[str, Any]:
        """
        Build a bounded-budget context view to reduce multi-agent prompt token overhead.
        Preserves high-priority fields (`goal`, `constraints`, `claims`, `summary`) and
        truncates bulky detail text to fit `byte_budget`.
        """
        if type(byte_budget) is not int or byte_budget < 2:
            raise ValueError("Context byte budget must be at least two bytes")
        data = self.get(ctx_id, reader_agent)
        clean_data = self._sanitize_for_json(data)
        raw_text = json.dumps(clean_data, ensure_ascii=False, default=str)
        raw_len = len(raw_text.encode("utf-8"))

        if raw_len <= byte_budget:
            return {
                "ctx_id": ctx_id,
                "truncated": False,
                "raw_bytes": raw_len,
                "view_bytes": raw_len,
                "content": clean_data,
            }

        def size(value):
            return len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))
        preserved = {}
        reference = {'_context_ref': ctx_id}
        if size(reference) <= byte_budget:
            preserved.update(reference)
        if isinstance(clean_data, dict):
            for key in ('goal', 'constraints', 'claims', 'summary', 'status'):
                if key in clean_data and size({**preserved, key: clean_data[key]}) <= byte_budget:
                    preserved[key] = clean_data[key]
        low, high = 0, min(len(raw_text), byte_budget)
        while low < high:
            middle = (low + high + 1) // 2
            if size({**preserved, '_truncated_preview': raw_text[:middle]}) <= byte_budget:
                low = middle
            else:
                high = middle - 1
        if size({**preserved, '_truncated_preview': raw_text[:low]}) <= byte_budget:
            preserved['_truncated_preview'] = raw_text[:low]
        return {'ctx_id': ctx_id, 'truncated': True, 'raw_bytes': raw_len,
                'view_bytes': size(preserved), 'content': preserved}

    def read_page(
        self,
        ctx_id: str,
        reader_agent: str,
        offset: int = 0,
        limit: int = 512,
    ) -> Dict[str, Any]:
        """Paginated read of large raw context content by authorized reference."""
        data = self.get(ctx_id, reader_agent)
        raw_text = json.dumps(data, ensure_ascii=False)
        start = max(0, int(offset))
        end = min(len(raw_text), start + max(1, int(limit)))
        return {
            "ctx_id": ctx_id,
            "offset": start,
            "limit": limit,
            "total_chars": len(raw_text),
            "has_more": end < len(raw_text),
            "slice": raw_text[start:end],
        }

    def grant_access_batch(
        self,
        ctx_ids: List[str],
        granter_agent: str,
        grantee_agent: str,
        retain_holder: bool = True,
    ) -> None:
        """Atomically validate and grant access (plus optional holder lease) across multiple context refs."""
        unique_ids = list(dict.fromkeys(ctx_ids))
        if not unique_ids:
            return
        with self._lock:
            # Phase 1: Validate ALL refs before mutating anything
            for cid in unique_ids:
                if cid not in self._meta:
                    raise KeyError(f"Context object '{cid}' not found")
                acl = self._acls.get(cid, set())
                if granter_agent not in acl:
                    raise PermissionError(f"Agent '{granter_agent}' cannot grant access to '{cid}'")

            # Phase 2: Commit all updates in a single SQLite transaction
            updates = []
            for cid in unique_ids:
                new_acl = set(self._acls.get(cid, set())) | {grantee_agent}
                new_holders = set(self._holders.get(cid) or ([self._meta[cid]["owner_agent"]] if self._meta[cid].get("owner_agent") else []))
                if retain_holder:
                    new_holders.add(grantee_agent)
                new_meta = {**self._meta[cid], "holders": sorted(new_holders)}
                updates.append((cid, new_meta, new_acl, new_holders))

            with self._write_tx() as db:
                for cid, new_meta, new_acl, _ in updates:
                    db.execute(
                        "INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                        (cid, json.dumps(new_meta), json.dumps(sorted(new_acl))),
                    )

            # Phase 3: Publish in memory only after commit succeeded
            for cid, new_meta, new_acl, new_holders in updates:
                self._meta[cid] = new_meta
                self._acls[cid] = new_acl
                self._holders[cid] = new_holders

    def release_agent(self, agent_name: str, *, strict=False, revoke_agents=()) -> None:
        """Release all holder leases owned by agent_name, deleting objects with no remaining holders."""
        with self._lock:
            held = [cid for cid, holders in list(self._holders.items()) if agent_name in holders]
        for cid in held:
            try:
                self.delete(cid, requester_agent=agent_name, revoke_agents=revoke_agents)
            except (PermissionError, OSError):
                if strict:
                    raise

    def delete(self, ctx_id: str, requester_agent: Optional[str] = None, *, revoke_agents=()) -> bool:
        """Detach holder and physically delete only when no holders remain; atomic across SQLite and disk."""
        with self._lock:
            meta = self._meta.get(ctx_id)
            if meta is None:
                return False
            holders = set(self._holders.get(ctx_id) or ([meta["owner_agent"]] if meta.get("owner_agent") else []))
            if requester_agent is not None:
                if requester_agent not in holders and meta.get("owner_agent") != requester_agent:
                    raise PermissionError(f"Agent '{requester_agent}' cannot delete context '{ctx_id}'")
                remaining_holders = holders - {requester_agent}
                if remaining_holders:
                    # Other active scopes/agents still reference this deduplicated/IPC-retained context: detach only.
                    updated_meta = {**meta, "holders": sorted(remaining_holders)}
                    updated_acl = set(self._acls.get(ctx_id, set())) - {requester_agent, *revoke_agents}
                    with self._write_tx() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                            (ctx_id, json.dumps(updated_meta), json.dumps(sorted(updated_acl))),
                        )
                    self._meta[ctx_id] = updated_meta
                    self._holders[ctx_id] = remaining_holders
                    self._acls[ctx_id] = updated_acl
                    return False

            # Last holder (or admin force-delete): commit SQLite DELETE first
            old_acl = sorted(self._acls.get(ctx_id, set()))
            with self._write_tx() as db:
                db.execute("DELETE FROM objects WHERE id=?", (ctx_id,))

            file_path = meta.get("file_path")
            if file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except OSError:
                    # Physical file still exists: restore SQLite row and raise so caller never sees false reclamation.
                    try:
                        with self._write_tx() as db:
                            db.execute(
                                "INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                                (ctx_id, json.dumps(meta), json.dumps(old_acl)),
                            )
                    except Exception:
                        pass
                    raise

            self._meta.pop(ctx_id, None)
            self._acls.pop(ctx_id, None)
            self._holders.pop(ctx_id, None)
            self._cache.pop(ctx_id, None)
            self._access_counts.pop(ctx_id, None)
            self.physical_bytes_stored = max(0, self.physical_bytes_stored - int(meta.get("stored_bytes", 0)))
            self.unique_objects = max(0, self.unique_objects - 1)
            return True

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            saved_ratio = (
                round(1.0 - (self.physical_bytes_stored / self.logical_bytes_written), 4)
                if self.logical_bytes_written > 0
                else 0.0
            )
            return {
                "logical_bytes_written": self.logical_bytes_written,
                "physical_bytes_stored": self.physical_bytes_stored,
                "index_bytes": os.path.getsize(self._index_path),
                "total_disk_bytes": self.physical_bytes_stored + os.path.getsize(self._index_path),
                "storage_saving_ratio": saved_ratio,
                "unique_objects": self.unique_objects,
                "dedup_hits": self.dedup_hits,
                "cache_hits": self.cache_hits,
            }
