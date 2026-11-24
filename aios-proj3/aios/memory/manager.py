# Project module contract / 项目模块说明
# 职责：管理记忆预算、检索与注入；跨Agent/用户访问以显式身份及策略为界。
# 主要接口：MemoryManager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
from collections import OrderedDict
import threading
import time
from typing import Any, Dict, List, Optional, Set

from cerebrum.memory.apis import MemoryQuery, MemoryResponse
from aios.config.config_manager import config as global_config
from aios.memory.amem import AMemEngine
from aios.memory.note import MemoryNote
from aios.memory.write_barrier import MemoryWriteBarrier


class MemoryManager:
    """
    AIOS Kernel Memory Manager.
    Provides user-scoped and agent-scoped memory storage, LRU-K eviction within a
    byte budget, keyword/semantic search, and read-after-write barrier coordination.
    """

    def __init__(
        self,
        memory_limit: Optional[int] = None,
        eviction_k: Optional[int] = None,
        log_mode: str = "console",
        provider: Optional[str] = None,
    ):
        mem_cfg = global_config.get_memory_config() or {}
        self.log_mode = log_mode
        self.provider_name = provider or mem_cfg.get("provider", "in-house")
        self.memory_limit = int(
            memory_limit if memory_limit is not None else mem_cfg.get("memory_limit", 104857600)
        )
        self.eviction_k = int(
            eviction_k if eviction_k is not None else mem_cfg.get("eviction_k", 2)
        )

        self._notes: Dict[str, MemoryNote] = {}
        self._known_user_ids: OrderedDict[str, float] = OrderedDict()
        self._used_bytes: int = 0
        self._lock = threading.RLock()

        barrier_cfg = mem_cfg.get("write_barrier", {}) or {}
        self.barrier = MemoryWriteBarrier(config=barrier_cfg)
        self.amem = AMemEngine()

    @property
    def known_user_ids(self) -> Set[str]:
        with self._lock:
            return set(self._known_user_ids.keys())

    @property
    def latest_user_id(self) -> Optional[str]:
        with self._lock:
            if not self._known_user_ids:
                return None
            return next(reversed(self._known_user_ids))

    def _register_user_id(self, user_id: str) -> None:
        self._known_user_ids[user_id] = time.monotonic()
        self._known_user_ids.move_to_end(user_id)

    def _evict_if_needed(self, incoming_bytes: int) -> int:
        evicted = 0
        while self._notes and (self._used_bytes + incoming_bytes > self.memory_limit):
            victim_id = min(
                self._notes.keys(),
                key=lambda mid: self._notes[mid].lru_k_score(self.eviction_k),
            )
            victim = self._notes.pop(victim_id)
            self._used_bytes = max(0, self._used_bytes - victim.size_bytes)
            evicted += 1
        return evicted

    def add_memory(
        self,
        content: str,
        user_id: str = "default",
        agent_id: str = "global",
        keywords: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryNote:
        note = MemoryNote(
            content=str(content),
            user_id=user_id or "default",
            agent_id=agent_id or "global",
            keywords=list(keywords or []),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
        )
        if note.size_bytes > self.memory_limit:
            raise ValueError(
                f"Memory note size ({note.size_bytes}B) exceeds kernel memory_limit ({self.memory_limit}B)"
            )
        with self._lock:
            self._evict_if_needed(note.size_bytes)
            self.amem.link_and_evolve(note, self._notes)
            self._notes[note.memory_id] = note
            self._used_bytes += note.size_bytes
            self._register_user_id(note.user_id)
        return note

    def get_memory(self, memory_id: str) -> Optional[MemoryNote]:
        with self._lock:
            note = self._notes.get(memory_id)
            if note is not None:
                note.touch(self.eviction_k)
            return note

    def remove_memory(self, memory_id: str) -> bool:
        with self._lock:
            note = self._notes.pop(memory_id, None)
            if note is None:
                return False
            self._used_bytes = max(0, self._used_bytes - note.size_bytes)
            return True

    def update_memory(self, memory_id: str, content: str) -> Optional[MemoryNote]:
        with self._lock:
            note = self._notes.get(memory_id)
            if note is None:
                return None
            old_bytes = note.size_bytes
            new_bytes = len(content.encode("utf-8"))
            if new_bytes > old_bytes:
                self._evict_if_needed(new_bytes - old_bytes)
            note.content = content
            note.updated_at = time.time()
            note.touch(self.eviction_k)
            self._used_bytes = max(0, self._used_bytes - old_bytes + new_bytes)
            return note

    def search_notes(
        self,
        query_text: str = "",
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        k: int = 5,
    ) -> List[Dict[str, Any]]:
        terms = [t.lower() for t in (query_text or "").split() if t.strip()]
        with self._lock:
            candidates: List[MemoryNote] = []
            for note in self._notes.values():
                if user_id and note.user_id != user_id:
                    continue
                if agent_id and note.agent_id != agent_id and note.metadata.get("policy") != "shared":
                    continue
                candidates.append(note)

            def score(n: MemoryNote) -> float:
                if not terms:
                    return n.updated_at
                hay = f"{n.content} {' '.join(n.keywords)} {' '.join(n.tags)}".lower()
                hits = sum(1 for t in terms if t in hay)
                return hits * 1000.0 + n.lru_k_score(self.eviction_k) * 1e-6

            ranked = sorted(candidates, key=score, reverse=True)[: max(1, int(k))]
            results_notes: List[MemoryNote] = []
            seen_ids: Set[str] = set()
            for n in ranked:
                if n.memory_id not in seen_ids:
                    n.touch(self.eviction_k)
                    results_notes.append(n)
                    seen_ids.add(n.memory_id)
                # A-Mem 1-hop associative link expansion
                for linked_id in n.links:
                    if len(results_notes) >= max(1, int(k)):
                        break
                    neighbor = self._notes.get(linked_id)
                    if neighbor is not None and neighbor.memory_id not in seen_ids:
                        if user_id and neighbor.user_id != user_id:
                            continue
                        if agent_id and neighbor.agent_id != agent_id and neighbor.metadata.get("policy") != "shared":
                            continue
                        neighbor.touch(self.eviction_k)
                        results_notes.append(neighbor)
                        seen_ids.add(neighbor.memory_id)
            return [n.to_dict() for n in results_notes[: max(1, int(k))]]

    def list_notes(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            notes = [
                n for n in self._notes.values() if user_id is None or n.user_id == user_id
            ]
            notes.sort(key=lambda n: n.updated_at, reverse=True)
            return [n.to_dict() for n in notes]

    def address_request(self, syscall: Any) -> MemoryResponse:
        query: MemoryQuery = syscall.query
        op = query.operation_type
        params = query.params or {}
        user_id = query.user_id or params.get("user_id") or getattr(syscall, "user_id", None) or "default"
        agent_id = syscall.agent_name

        # Wait on write barrier if this is a read operation
        if op in ("get_memory", "retrieve_memory", "retrieve_memory_raw"):
            target_seq = getattr(syscall, "barrier_seq", None)
            self.barrier.wait_for_seq(user_id, target_seq)

        try:
            if op in ('get_memory', 'update_memory', 'remove_memory'):
                mid = params.get('memory_id') or params.get('id') or ''
                with self._lock:
                    note = self._notes.get(mid)
                    permitted = note is not None and note.user_id == user_id and (
                        note.agent_id == agent_id or op == 'get_memory' and note.metadata.get('policy') == 'shared')
                if not permitted:
                    return MemoryResponse(error='Memory access denied', status_code=403)
            if op == "add_memory":
                content = params.get("content") or params.get("text") or ""
                note = self.add_memory(
                    content=content,
                    user_id=user_id,
                    agent_id=agent_id,
                    keywords=params.get("keywords"),
                    tags=params.get("tags"),
                    metadata=params.get("metadata"),
                )
                return MemoryResponse(
                    response_message=f"Memory added: {note.memory_id}",
                    memory_id=note.memory_id,
                    finished=True,
                )

            if op == "get_memory":
                mid = params.get("memory_id") or params.get("id") or ""
                note = self.get_memory(mid)
                if note is None:
                    return MemoryResponse(
                        response_message="Memory not found",
                        finished=True,
                        status_code=404,
                    )
                return MemoryResponse(
                    response_message=note.content,
                    memory_id=note.memory_id,
                    search_results=[note.to_dict()],
                    finished=True,
                )

            if op == "update_memory":
                mid = params.get("memory_id") or params.get("id") or ""
                content = params.get("content") or ""
                note = self.update_memory(mid, content)
                if note is None:
                    return MemoryResponse(
                        response_message="Memory not found",
                        finished=True,
                        status_code=404,
                    )
                return MemoryResponse(
                    response_message=f"Memory updated: {note.memory_id}",
                    memory_id=note.memory_id,
                    finished=True,
                )

            if op == "remove_memory":
                mid = params.get("memory_id") or params.get("id") or ""
                ok = self.remove_memory(mid)
                return MemoryResponse(
                    response_message="Memory removed" if ok else "Memory not found",
                    memory_id=mid if ok else None,
                    finished=True,
                    status_code=200 if ok else 404,
                )

            if op in ("retrieve_memory", "retrieve_memory_raw"):
                q_text = params.get("query") or params.get("content") or ""
                k = int(params.get("k", 5))
                results = self.search_notes(query_text=q_text, user_id=user_id, agent_id=agent_id, k=k)
                return MemoryResponse(
                    response_message=results,
                    search_results=results,
                    finished=True,
                )

            if op == "clear_memories":
                with self._lock:
                    to_del = [mid for mid, n in self._notes.items() if n.user_id == user_id and n.agent_id == agent_id]
                    for mid in to_del:
                        victim = self._notes.pop(mid)
                        self._used_bytes = max(0, self._used_bytes - victim.size_bytes)
                return MemoryResponse(
                    response_message=f"Cleared {len(to_del)} memories for user '{user_id}'",
                    finished=True,
                )

            return MemoryResponse(
                response_message=f"Unsupported memory operation: {op}",
                finished=True,
                error=f"Unsupported memory operation: {op}",
                status_code=400,
            )
        except Exception as exc:
            return MemoryResponse(
                response_message=str(exc),
                finished=True,
                error=str(exc),
                status_code=500,
            )
