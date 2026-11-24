# Project module contract / 项目模块说明
# 职责：管理Agent消息、授权引用、确认和撤销；失败不可静默变成成功投递。
# 主要接口：IPCMessage, IPCManager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
import copy
from dataclasses import dataclass, field
import json
import secrets
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class IPCMessage:
    message_id: str
    request_id: str
    sender: str
    recipient: str
    payload: Any
    context_refs: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    acked: bool = False
    delivery_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "request_id": self.request_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": copy.deepcopy(self.payload),
            "context_refs": list(self.context_refs),
            "created_at": self.created_at,
            "acked": self.acked,
            "delivery_count": self.delivery_count,
        }


class IPCManager:
    """
    AIOS Kernel Inter-Agent Mailbox Broker (Proj3 Requirement 4.7).
    Implements:
    - Bounded per-Agent mailboxes with ephemeral capability credentials
    - Request-ID idempotency & conflict detection
    - At-least-once delivery (`receive` / `ack`)
    - Zero-copy context reference passing (`context_refs`) with automatic ACL delegation
      via `ContextStore`
    """

    def __init__(self, context_store: Any = None, max_inbox_size: int = 128):
        self.context_store = context_store
        self.max_inbox_size = max_inbox_size
        self._lock = threading.RLock()
        self._inboxes: Dict[str, List[IPCMessage]] = {}
        self._request_ledger: Dict[str, Tuple[str, IPCMessage]] = {}
        self._tokens: Dict[str, str] = {}
        self._revoked_agents: Set[str] = set()

    def issue_token(self, agent_name: str) -> str:
        with self._lock:
            token = f"ipc_tok_{secrets.token_hex(12)}"
            self._tokens[agent_name] = token
            self._revoked_agents.discard(agent_name)
            return token

    def revoke_token(self, agent_name: str) -> None:
        with self._lock:
            self._tokens.pop(agent_name, None)
            self._revoked_agents.add(agent_name)

    def verify_token(self, agent_name: str, token: Optional[str]) -> None:
        with self._lock:
            if agent_name in self._revoked_agents:
                raise PermissionError(f"IPC token revoked for agent '{agent_name}'")
            if token is not None:
                if self._tokens.get(agent_name) != token:
                    raise PermissionError(f"Invalid or revoked IPC token for agent '{agent_name}'")
            elif agent_name in self._tokens:
                raise PermissionError(f"Missing IPC token for agent '{agent_name}'")

    @staticmethod
    def _fingerprint(sender: str, recipient: str, payload: Any, refs: List[str]) -> str:
        return json.dumps(
            {"s": sender, "r": recipient, "p": payload, "refs": sorted(refs)},
            sort_keys=True,
            ensure_ascii=False,
        )

    def send(
        self,
        sender: str,
        recipient: str,
        payload: Any,
        request_id: Optional[str] = None,
        context_refs: Optional[List[str]] = None,
        grant_refs: bool = True,
        token: Optional[str] = None,
    ) -> Dict[str, Any]:
        req_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        refs = list(context_refs or [])
        fp = self._fingerprint(sender, recipient, payload, refs)

        with self._lock:
            if token is not None or sender in self._tokens or sender in self._revoked_agents:
                self.verify_token(sender, token)

            if req_id in self._request_ledger:
                existing_fp, existing_msg = self._request_ledger[req_id]
                if existing_fp != fp:
                    raise ValueError(
                        f"Idempotency conflict: duplicate request_id '{req_id}' with different payload"
                    )
                return existing_msg.to_dict()

            inbox = self._inboxes.setdefault(recipient, [])
            unacked_count = sum(1 for m in inbox if not m.acked)
            if unacked_count >= self.max_inbox_size:
                raise RuntimeError(f"Mailbox full for recipient agent '{recipient}'")

            if grant_refs and self.context_store is not None and refs:
                if hasattr(self.context_store, "grant_access_batch"):
                    self.context_store.grant_access_batch(refs, sender, recipient, retain_holder=True)
                else:
                    for ctx_id in refs:
                        self.context_store.grant_access(ctx_id, sender, recipient)

            msg = IPCMessage(
                message_id=f"msg_{uuid.uuid4().hex[:12]}",
                request_id=req_id,
                sender=sender,
                recipient=recipient,
                payload=copy.deepcopy(payload),
                context_refs=refs,
            )
            inbox.append(msg)
            self._request_ledger[req_id] = (fp, msg)
            return msg.to_dict()

    def receive(
        self,
        recipient: str,
        limit: int = 10,
        include_unacked: bool = True,
        token: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            if token is not None or recipient in self._tokens or recipient in self._revoked_agents:
                self.verify_token(recipient, token)

            inbox = self._inboxes.get(recipient, [])
            results: List[Dict[str, Any]] = []
            for msg in inbox:
                if msg.acked:
                    continue
                if msg.delivery_count > 0 and not include_unacked:
                    continue
                msg.delivery_count += 1
                results.append(msg.to_dict())
                if len(results) >= max(1, limit):
                    break
            return results

    def ack(
        self,
        recipient: str,
        message_id: str,
        token: Optional[str] = None,
    ) -> bool:
        with self._lock:
            if token is not None or recipient in self._tokens or recipient in self._revoked_agents:
                self.verify_token(recipient, token)

            for msg in self._inboxes.get(recipient, []):
                if msg.message_id == message_id:
                    msg.acked = True
                    return True
            return False

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_requests": len(self._request_ledger),
                "mailboxes": {
                    agent: {
                        "total": len(msgs),
                        "pending": sum(1 for m in msgs if not m.acked),
                        "acked": sum(1 for m in msgs if m.acked),
                    }
                    for agent, msgs in self._inboxes.items()
                },
            }
