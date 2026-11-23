from dataclasses import dataclass, field
import re
import time
import uuid
from typing import Any, Dict, List


def extract_salient_terms(text: str, limit: int = 6) -> List[str]:
    """Extract salient alphanumeric and CJK terms for A-Mem note indexing."""
    tokens = re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", (text or "").lower())
    stop = {"the", "and", "for", "with", "this", "that", "from", "into", "用户", "可以", "一个", "我们"}
    seen: List[str] = []
    for tok in tokens:
        if tok not in stop and tok not in seen:
            seen.append(tok)
        if len(seen) >= limit:
            break
    return seen


@dataclass
class MemoryNote:
    """
    Atomic Zettelkasten memory unit in the AIOS Kernel Memory Manager (A-Mem: arXiv 2502.12110).
    Contains raw content, extracted keywords, contextual summary, tags, bidirectional links
    to semantically related notes, and evolution counter.
    """
    content: str
    memory_id: str = field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    user_id: str = "default"
    agent_id: str = "global"
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    context_summary: str = ""
    links: List[str] = field(default_factory=list)
    evolution_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    access_history: List[float] = field(default_factory=lambda: [time.monotonic()])

    def __post_init__(self) -> None:
        if not self.keywords:
            self.keywords = extract_salient_terms(self.content, limit=6)
        if not self.tags:
            self.tags = list(self.keywords[:3])
        if not self.context_summary:
            self.context_summary = f"[{', '.join(self.tags)}] {self.content[:120]}"

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    def touch(self, k: int = 2) -> None:
        self.access_history.append(time.monotonic())
        if len(self.access_history) > max(k, 1) * 2:
            self.access_history = self.access_history[-max(k, 1) * 2 :]

    def lru_k_score(self, k: int = 2) -> float:
        if len(self.access_history) >= k:
            return self.access_history[-k]
        return self.access_history[0] * 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.memory_id,
            "content": self.content,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "keywords": list(self.keywords),
            "tags": list(self.tags),
            "context_summary": self.context_summary,
            "links": list(self.links),
            "evolution_count": self.evolution_count,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
