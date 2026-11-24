# Project module contract / 项目模块说明
# 职责：管理记忆预算、检索与注入；跨Agent/用户访问以显式身份及策略为界。
# 主要接口：AMemEngine。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""
A-Mem: Agentic Memory for LLM Agents (arXiv:2502.12110) Engine for AIOS Kernel.
Implements Zettelkasten-inspired memory organization:
1. Note Construction (content + keywords + tags + context_summary)
2. Dynamic Link Generation (connecting new notes to semantically related existing notes)
3. Memory Evolution (refining existing neighbor notes' tags, keywords & contextual summaries)
4. Associative Graph Retrieval (1-hop link expansion during memory recall)
"""
import time
from typing import Dict, List, Set
from aios.memory.note import MemoryNote, extract_salient_terms


class AMemEngine:
    def __init__(self, similarity_threshold: float = 0.15, max_links_per_note: int = 4):
        self.similarity_threshold = similarity_threshold
        self.max_links_per_note = max_links_per_note

    @staticmethod
    def compute_similarity(note_a: MemoryNote, note_b: MemoryNote) -> float:
        kw_a: Set[str] = set([w.lower() for w in note_a.keywords] + [t.lower() for t in note_a.tags])
        kw_b: Set[str] = set([w.lower() for w in note_b.keywords] + [t.lower() for t in note_b.tags])
        kw_sim = (len(kw_a & kw_b) / len(kw_a | kw_b)) if (kw_a and kw_b) else 0.0

        terms_a: Set[str] = kw_a | set(extract_salient_terms(note_a.content, limit=12))
        terms_b: Set[str] = kw_b | set(extract_salient_terms(note_b.content, limit=12))
        if not terms_a or not terms_b:
            return 0.0
        term_sim = len(terms_a & terms_b) / len(terms_a | terms_b)
        return max(kw_sim, term_sim)

    def link_and_evolve(self, new_note: MemoryNote, existing_notes: Dict[str, MemoryNote]) -> List[str]:
        """
        Find related notes for `new_note` within the same `user_id`, establish bidirectional
        Zettelkasten links, and evolve neighbor notes' tags & context summaries.
        """
        scored: List[tuple[float, MemoryNote]] = []
        for other in existing_notes.values():
            if other.memory_id == new_note.memory_id or other.user_id != new_note.user_id:
                continue
            if other.agent_id != new_note.agent_id and not (
                    other.metadata.get('policy') == 'shared' and new_note.metadata.get('policy') == 'shared'):
                continue
            sim = self.compute_similarity(new_note, other)
            if sim >= self.similarity_threshold:
                scored.append((sim, other))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        linked_ids: List[str] = []

        for _, neighbor in scored[: self.max_links_per_note]:
            if neighbor.memory_id not in new_note.links:
                new_note.links.append(neighbor.memory_id)
            if new_note.memory_id not in neighbor.links:
                neighbor.links.append(new_note.memory_id)
            linked_ids.append(neighbor.memory_id)

            # Memory Evolution: merge new salient tags/keywords into neighbor and update summary
            merged_tags = list(dict.fromkeys(neighbor.tags + [t for t in new_note.tags if t not in neighbor.tags]))[:6]
            merged_kw = list(
                dict.fromkeys(neighbor.keywords + [k for k in new_note.keywords if k not in neighbor.keywords])
            )[:8]
            neighbor.tags = merged_tags
            neighbor.keywords = merged_kw
            neighbor.evolution_count += 1
            neighbor.updated_at = time.time()
            neighbor.context_summary = (
                f"[{', '.join(neighbor.tags)}] {neighbor.content[:100]} "
                f"(evolved#{neighbor.evolution_count} <- {new_note.memory_id})"
            )

        return linked_ids
