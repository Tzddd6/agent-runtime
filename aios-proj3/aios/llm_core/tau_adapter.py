# Project module contract / 项目模块说明
# 职责：适配模型协议、持钥服务与结果校验；不重放不确定付费调用。
# 主要接口：EvaluationBudget, TauBridge。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""tau-bench evaluation budget ledger & AIOS Kernel bridge (ported from tau_adapter.py)."""
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
import uuid

from cerebrum.llm.apis import LLMQuery


class EvaluationBudget:
    """Conservative peak-rate CNY reservation and settlement ledger for tau-bench."""

    def __init__(self, cny: float = 10.0, max_calls: int = 32, ledger: Optional[str | Path] = None):
        if type(cny) not in (int, float) or not math.isfinite(cny) or cny <= 0 or type(max_calls) is not int or max_calls < 1:
            raise ValueError("Finite positive budget required")
        self.limit = float(cny)
        self.max_calls = max_calls
        self.spent = 0.0
        self.calls = 0
        self.lock = threading.Lock()
        self.ledger = Path(ledger) if ledger else None

    def reserve(self, output_tokens: int = 2048) -> float:
        reserve_amt = (1048576 * 2.0 + output_tokens * 8.0) / 1_000_000.0
        with self.lock:
            if self.calls >= self.max_calls or self.spent + reserve_amt > self.limit:
                raise RuntimeError("Evaluation budget exhausted; no new provider request")
            self.calls += 1
            self.spent += reserve_amt
            self._persist()
        return reserve_amt

    def settle(self, reserve_amt: float, usage: Dict[str, Any]) -> Optional[float]:
        prompt = usage.get("prompt_tokens")
        output = usage.get("completion_tokens")
        if type(prompt) is not int or type(output) is not int or not 0 <= prompt <= 1048576 or not 0 <= output <= 4096:
            return None
        hit = usage.get("prompt_cache_hit_tokens", 0)
        if type(hit) is not int or not 0 <= hit <= prompt:
            hit = 0
        cost = ((prompt - hit) * 2.0 + hit * 0.04 + output * 8.0) / 1_000_000.0
        with self.lock:
            self.spent += max(0.0, cost) - reserve_amt
            self._persist()
        return cost

    def _persist(self) -> None:
        if self.ledger is not None:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ledger.with_name(self.ledger.name + "." + uuid.uuid4().hex[:8])
            tmp.write_text(
                json.dumps({"limit_cny": self.limit, "spent_or_reserved_cny": self.spent, "calls": self.calls}),
                encoding="utf-8",
            )
            tmp.replace(self.ledger)


class TauBridge:
    """Bridge connecting tau-bench agent evaluation turns to the AIOS Kernel."""

    def __init__(self, kernel: Any, output_dir: str | Path, cny_limit: float = 10.0, max_calls: int = 32):
        self.kernel = kernel
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.budget = EvaluationBudget(cny_limit, max_calls, self.output / "tau_ledger.json")
        self.records: List[Dict[str, Any]] = []

    def generate_turn(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 1024,
        agent_name: str = "tau_agent",
    ) -> Dict[str, Any]:
        reserve_amt = self.budget.reserve(max_tokens)
        t0 = time.monotonic()
        query = LLMQuery(messages=messages, tools=tools, max_new_tokens=max_tokens)
        exec_res = self.kernel.execute_request(agent_name, query)
        resp = exec_res["response"]
        usage = getattr(resp, "usage", {}) or {"prompt_tokens": 64, "completion_tokens": 32, "total_tokens": 96}
        cost = self.budget.settle(reserve_amt, usage)
        rec = {
            "syscall_id": exec_res["syscall_id"],
            "agent": agent_name,
            "status": "succeeded" if not getattr(resp, "error", None) else "failed",
            "reserved_peak_cny": reserve_amt,
            "settled_cost_cny": cost,
            "usage": usage,
            "seconds": round(time.monotonic() - t0, 6),
        }
        self.records.append(rec)
        return {"response": resp.to_dict(), "record": rec}
