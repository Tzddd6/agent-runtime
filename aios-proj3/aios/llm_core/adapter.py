# Project module contract / 项目模块说明
# 职责：适配模型协议、持钥服务与结果校验；不重放不确定付费调用。
# 主要接口：LLMConfig, LLMAdapter。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
from dataclasses import dataclass
import json
import logging
from typing import Any, Dict, List, Optional, Union
import urllib.request

from cerebrum.llm.apis import LLMQuery, LLMResponse
from aios.config.config_manager import config
from aios.context.simple_context import SimpleContextManager
from aios.llm_core.profiles import ModelProfileBroker, provider_endpoint
from aios.llm_core.routing import RouterStrategy, SequentialRouting, SmartRouting
from aios.llm_core.streaming import (
    encode_responses_input,
    parse_chat_completions_sse_stream,
    parse_responses_sse_stream,
)
from aios.utils.id_generator import generator_tool_call_id

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    name: str
    backend: Optional[str] = "mock"
    max_gpu_memory: Optional[str] = None
    eval_device: Optional[str] = None
    hostname: Optional[str] = None
    api_key: Optional[str] = None


class LLMAdapter:
    """
    AIOS Kernel LLM Core Adapter.
    Treats LLM backends (OpenAI/DeepSeek API, Ollama, vLLM, Local/Mock) as unified CPU cores,
    supporting routing strategies, tool-calling formatting, and context switching via
    SimpleContextManager.
    """

    def __init__(
        self,
        llm_configs: Optional[List[Dict[str, Any]]] = None,
        api_key: Optional[Union[str, List[str]]] = None,
        log_mode: str = "console",
        use_context_manager: bool = True,
        routing_strategy: RouterStrategy = RouterStrategy.Sequential,
    ):
        self.log_mode = log_mode
        self.use_context_manager = use_context_manager
        self.context_manager = SimpleContextManager() if use_context_manager else None
        if self.context_manager:
            self.context_manager.start()

        if llm_configs is None:
            llm_configs = config.get_llms_config().get("models") or [
                {"name": "mock-aios-core", "backend": "mock"}
            ]
        self.llm_configs = llm_configs
        self.routing_strategy = routing_strategy
        self.profile_broker = ModelProfileBroker()
        self.stream_observer = None
        if routing_strategy == RouterStrategy.Smart:
            self.router = SmartRouting(self.llm_configs)
        else:
            self.router = SequentialRouting(self.llm_configs)

    def _build_mock_tokens(self, query: LLMQuery, model_name: str) -> List[str]:
        last_user = ""
        sys_notes = []
        for msg in query.messages:
            if msg.get("role") == "user":
                last_user = str(msg.get("content", ""))
            elif msg.get("role") == "system":
                sys_notes.append(str(msg.get("content", "")))
        context_tag = f" [ctx:{len(sys_notes)}]" if sys_notes else ""
        reply = f"[{model_name}]{context_tag} Processed: {last_user}"
        # Split into word tokens for realistic streaming / time-slice preemption
        words = reply.split(" ")
        return [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]

    def _call_openai_compatible(
        self,
        model_cfg: Dict[str, Any],
        query: LLMQuery,
    ) -> LLMResponse:
        backend = (model_cfg.get("backend") or "openai").lower()
        hostname = provider_endpoint(backend, model_cfg.get("hostname") or "")
        api_key = model_cfg.get("api_key") or config.get_api_key(backend)
        payload: Dict[str, Any] = {
            "model": model_cfg.get("name"),
            "messages": query.messages,
            "temperature": query.temperature,
            "max_tokens": query.max_new_tokens,
        }
        if query.tools:
            payload["tools"] = query.tools
        if query.seed is not None:
            payload["seed"] = query.seed
        if query.tool_choice is not None:
            payload["tool_choice"] = query.tool_choice

        headers = {"Content-Type": "application/json"}

        if backend == "aizex" or model_cfg.get("protocol") == "responses":
            if query.seed is not None:
                raise ValueError("Responses seed parity is unsupported; refusing benchmark request")
            resp_payload = encode_responses_input(query.messages, tools=query.tools)
            if query.tool_choice is not None:
                resp_payload["tool_choice"] = query.tool_choice
            resp_payload.update(
                model=model_cfg.get("name"),
                max_output_tokens=query.max_new_tokens,
                stream=True,
                store=False,
            )
            req = urllib.request.Request(
                f"{hostname}/responses",
                data=json.dumps(resp_payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            if api_key:
                req.add_unredirected_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                parsed = parse_responses_sse_stream(
                    resp,
                    default_model=model_cfg.get("name", "gpt-6-luna"),
                    observer=self.stream_observer,
                )
            if parsed.get("finish_reason") not in ("stop", "tool_calls"):
                return LLMResponse(error="Incomplete model output; no replay", status_code=502, usage=parsed.get("usage") or {})
            return LLMResponse(
                response_message=parsed["content"],
                tool_calls=parsed.get("tool_calls"),
                finished=True,
                status_code=200,
                usage=parsed.get("usage") or {},
            )

        if query.stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        req = urllib.request.Request(
            f"{hostname}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        if api_key:
            req.add_unredirected_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            if query.stream or "text/event-stream" in resp.headers.get("Content-Type", ""):
                parsed = parse_chat_completions_sse_stream(
                    resp,
                    default_model=model_cfg.get("name", "deepseek-chat"),
                    observer=self.stream_observer,
                )
                return LLMResponse(
                    response_message=parsed["content"],
                    tool_calls=parsed.get("tool_calls"),
                    finished=True,
                    status_code=200,
                    usage=parsed.get("usage") or {},
                )
            body = json.loads(resp.read().decode("utf-8"))
        choice = (body.get("choices") or [{}])[0]
        if choice.get("finish_reason") not in ("stop", "tool_calls"):
            return LLMResponse(error="Incomplete model output; no replay", status_code=502, usage=body.get("usage") or {})
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls")
        return LLMResponse(
            response_message=message.get("content") or "",
            tool_calls=tool_calls,
            finished=True,
            status_code=200,
            usage=body.get("usage") or {},
        )

    def _select_model_config(self, requested_model: Optional[str]) -> Dict[str, Any]:
        for model in self.llm_configs:
            if model.get("name") == requested_model and model.get("backend") == "mock":
                return model
        broker_status = self.profile_broker.status()
        if broker_status.get("configured") and requested_model in (None, broker_status.get("model")):
            prof_id = broker_status.get("profile_id")
            if prof_id == "legacy":
                return {
                    "name": self.profile_broker.legacy_model,
                    "backend": "deepseek",
                    "hostname": "https://api.deepseek.com/v1",
                    "api_key": self.profile_broker._legacy_key,
                }
            prof = self.profile_broker.profiles.get(prof_id or "")
            if prof:
                provider = prof["provider"]
                return {
                    "name": prof["model"],
                    "backend": provider,
                    "hostname": prof["base_url"],
                    "api_key": prof["key"],
                    "protocol": "responses" if provider == "aizex" else "chat_completions",
                }
        return self.router.select(requested_model)

    def execute_llm_syscall(
        self,
        syscall: Any,
        time_limit: Optional[float] = None,
    ) -> LLMResponse:
        query: LLMQuery = syscall.query
        service = getattr(self, 'model_service', None)
        if service is not None and query.model != 'mock-aios-core':
            return service.complete(query)
        model_cfg = getattr(syscall, "selected_model_config", None)
        if model_cfg is None:
            model_cfg = self._select_model_config(query.model)
            syscall.selected_model_config = model_cfg
        model_name = model_cfg.get("name", "mock-aios-core")
        backend = (model_cfg.get("backend") or "mock").lower()
        effective_limit = time_limit if time_limit is not None else syscall.get_time_limit()

        try:
            if query.action_type == "tool_use" and query.tools and backend == "mock":
                first_tool = query.tools[0].get("function", {})
                tc_name = first_tool.get("name", "unknown_tool")
                return LLMResponse(
                    response_message="",
                    tool_calls=[
                        {
                            "id": generator_tool_call_id(),
                            "name": tc_name,
                            "parameters": {},
                        }
                    ],
                    finished=True,
                    status_code=200,
                )

            if backend in ("openai", "deepseek", "aizex", "vllm", "ollama"):
                if backend not in ("vllm", "ollama") and not (model_cfg.get("api_key") or config.get_api_key(backend)):
                    return LLMResponse(error="Model provider credential is missing", status_code=503)
                return self._call_openai_compatible(model_cfg, query)

            # Deterministic local/mock execution with optional RR context switching
            tokens = self._build_mock_tokens(query, model_name)[:query.max_new_tokens]
            per_token_delay = float(getattr(query, "per_token_delay", 0.0) or 0.0)
            if self.context_manager and effective_limit is not None:
                pid = syscall.get_pid() or id(syscall)
                text, finished = self.context_manager.save_context(
                    pid=pid,
                    model_name=model_name,
                    messages=query.messages,
                    token_stream=tokens,
                    time_limit=effective_limit,
                    per_token_delay=per_token_delay,
                )
                return LLMResponse(
                    response_message=text,
                    finished=finished,
                    status_code=200,
                    usage={"total_tokens": len(tokens)},
                )

            return LLMResponse(
                response_message="".join(tokens),
                finished=True,
                status_code=200,
                usage={"total_tokens": len(tokens)},
            )
        except Exception as exc:
            logger.error("LLM syscall execution error: %s", type(exc).__name__)
            return LLMResponse(
                response_message="",
                finished=True,
                error="Model request could not be completed",
                status_code=500,
            )
        finally:
            if isinstance(self.router, SmartRouting):
                self.router.release(model_name)

    def execute_llm_syscalls(self, batch: List[Any]) -> List[LLMResponse]:
        return [self.execute_llm_syscall(sc) for sc in batch]

    # Alias for compatibility with BaseScheduler
    address_request = execute_llm_syscall
