"""
AIOS Terminal UI Intent Router (ICLR 2025: From Commands to Prompts - LSFS).
Translates natural-language user prompts into structured AIOS StorageQuery syscalls
(create_file, write, read, retrieve, rollback, share, delete_file).
"""
import re
from typing import Any, Dict
from cerebrum.storage.apis import StorageQuery


class IntentRouter:
    """
    Semantic Prompt-to-Syscall Router for LSFS Terminal UI.
    Enables users and agents to interact with the file system using natural language
    prompts instead of rigid POSIX shell commands.
    """

    @staticmethod
    def route_prompt(prompt: str) -> StorageQuery:
        text = (prompt or "").strip()
        low = text.lower()

        # 1. Rollback intent: e.g. "rollback docs/arch.md by 2 versions" or "回滚 docs/arch.md 1 个版本"
        if "rollback" in low or "回滚" in low or "恢复版本" in low:
            path_match = re.search(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text)
            step_match = re.search(r"\b(\d+)\b", text)
            file_path = path_match.group(1) if path_match else "unknown.txt"
            steps = int(step_match.group(1)) if step_match else 1
            return StorageQuery(operation_type="rollback", params={"file_path": file_path, "n": steps})

        # 2. Share intent: e.g. "share docs/arch.md with auditor_agent" or "分享 docs/arch.md 给 auditor_agent"
        if "share" in low or "分享" in low or "共享" in low:
            path_match = re.search(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text)
            agent_match = re.search(r"(?:with|to|给)\s+([A-Za-z0-9_-]+)", text, re.IGNORECASE)
            file_path = path_match.group(1) if path_match else "unknown.txt"
            target_agent = agent_match.group(1) if agent_match else "public"
            return StorageQuery(
                operation_type="share",
                params={"file_path": file_path, "target_agent": target_agent},
            )

        # 3. Write intent: e.g. "write 'Hello AIOS' to notes/greeting.txt" or "write file a.txt with Hello"
        if low.startswith("write ") or "写入" in low or "保存" in low:
            path_match = re.search(r"(?:to|into|到)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text, re.IGNORECASE)
            quote_match = re.search(r"['\"]([^'\"]+)['\"]", text)
            if path_match and quote_match:
                return StorageQuery(
                    operation_type="write",
                    params={"file_path": path_match.group(1), "content": quote_match.group(1)},
                )
            alt_match = re.search(
                r"(?:file\s+)?([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)\s+(?:with|content)\s+(.+)$",
                text,
                re.IGNORECASE,
            )
            if alt_match:
                return StorageQuery(
                    operation_type="write",
                    params={"file_path": alt_match.group(1), "content": alt_match.group(2).strip().strip("'\"")},
                )

        # 4. Read intent: e.g. "read docs/arch.md" or "读取 docs/arch.md"
        if (low.startswith("read ") or low.startswith("cat ") or "读取" in low or "查看文件" in low) and re.search(
            r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text
        ):
            path_match = re.search(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text)
            return StorageQuery(
                operation_type="read",
                params={"file_path": path_match.group(1) if path_match else ""},
            )

        # 5. Default semantic retrieval intent ("retrieve / find / search / 查找 / 检索")
        clean_q = re.sub(
            r"^(?:retrieve|find|search|files|about|查找|检索|搜索|关于)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        return StorageQuery(
            operation_type="retrieve",
            params={"query_text": clean_q or text, "k": 3},
        )

    def execute_prompt(self, kernel: Any, prompt: str, agent_name: str = "terminal_user") -> Dict[str, Any]:
        query = self.route_prompt(prompt)
        res = kernel.execute_request(agent_name, query)
        return {
            "prompt": prompt,
            "mapped_syscall": query.operation_type,
            "params": query.params,
            "response": res["response"].to_dict() if hasattr(res["response"], "to_dict") else str(res["response"]),
        }
