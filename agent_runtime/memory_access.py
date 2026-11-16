"""Trusted coordinator memory selection over ContextStore ACLs.

Provider metadata must come from trusted storage, never model-supplied identity.
This component is not a web authentication system. Token budget is a conservative
UTF-8 byte bound (one byte costs one budget unit), not a supplier tokenizer.
"""
import json
import math
from .context import ContextStore


def principal(user, agent):
    return json.dumps([user, agent], ensure_ascii=False, separators=(',', ':'))


class MemoryContext:
    def __init__(self, root):
        self.store = ContextStore(root)

    @staticmethod
    def allowed(metadata, user, agent):
        return (isinstance(metadata, dict)
                and isinstance(metadata.get('user_id'), str)
                and bool(metadata['user_id']) and metadata['user_id'] == user
                and isinstance(metadata.get('owner_agent'), str)
                and bool(metadata['owner_agent'])
                and (metadata['owner_agent'] == agent
                     or metadata.get('sharing_policy') == 'shared'))

    def select(self, candidates, *, user, agent, max_bytes=5000,
               max_memories=20, relevance_threshold=0.0):
        if not isinstance(user, str) or not user or not isinstance(agent, str) or not agent:
            raise ValueError('Trusted user and agent identities are required')
        if type(max_bytes) is not int or max_bytes < 0 or type(max_memories) is not int or max_memories < 0:
            raise ValueError('Invalid memory budget')
        if not isinstance(candidates, list) or len(candidates) > 10000:
            raise ValueError('Too many memory candidates')
        if (not isinstance(relevance_threshold, (int, float))
                or not math.isfinite(relevance_threshold)):
            raise ValueError('Invalid relevance threshold')
        authorized = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            meta = candidate.get('metadata')
            if not self.allowed(meta, user, agent):
                continue
            content = candidate.get('content')
            score = candidate.get('score', 0)
            if (not isinstance(content, str) or not content
                    or not isinstance(score, (int, float)) or isinstance(score, bool)
                    or not math.isfinite(score) or score < relevance_threshold):
                continue
            # Auth precedes ranking, deduplication and budget accounting.
            authorized.append((score, candidate))
        authorized.sort(key=lambda row: row[0], reverse=True)
        chosen = []; seen = set(); used = 0
        for _, candidate in authorized:
            content = candidate['content']
            if content in seen:
                continue
            stamp = candidate.get('timestamp', '')
            if not isinstance(stamp, str):
                stamp = ''
            line = '- [' + stamp.replace('\n', ' ').replace(']', '') + '] ' + content + '\n'
            cost = len(line.encode('utf-8'))
            if cost + used > max_bytes or len(chosen) >= max_memories:
                continue
            meta = candidate['metadata']
            owner = principal(user, meta['owner_agent'])
            ref = self.store.put({'content': content, 'user_id': user,
                                  'owner_agent': meta['owner_agent']}, [owner])
            requester = principal(user, agent)
            if owner != requester:
                self.store.grant(ref, owner, requester)
            value = self.store.get(ref, requester)
            chosen.append({'content': value['content'], 'line': line, 'ref': ref})
            seen.add(content); used += cost
        return chosen, {'injected_count': len(chosen), 'memory_bytes': used,
                        'budget_kind': 'utf8_bytes', 'context_store': self.store.stats()}
