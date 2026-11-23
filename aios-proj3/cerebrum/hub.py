"""
Cerebrum Agent Hub & "LLM as OS, Agents as Apps" Ecosystem (NAACL 2025 & arXiv:2312.03815).
Treats Agents as portable, permission-scoped Applications ("Apps") running on the AIOS Kernel:
- AgentManifest: declares app metadata, required syscall categories, allowed tools, and OS resource quotas.
- ScopedAppClient: capability-restricted CerebrumClient enforcing the App's manifest boundaries.
- AgentHub: registry for publishing, discovering, loading, and launching Agent Apps on AIOS.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from cerebrum.client import CerebrumClient
from aios.scheduler.drf_admission import ResourceVector
from cerebrum.tool.apis import ToolQuery


@dataclass
class AgentManifest:
    """Declarative manifest for an AIOS Agent Application ('Agents as Apps')."""
    app_id: str  # e.g. "openagi/code_auditor"
    name: str
    version: str = "1.0.0"
    author: str = "aios_developer"
    description: str = ""
    required_syscalls: List[str] = field(default_factory=lambda: ["llm", "memory", "storage", "tool"])
    allowed_tools: Optional[List[str]] = None
    resource_quota: Dict[str, int] = field(
        default_factory=lambda: {
            "cpu_milli": 500,
            "memory_bytes": 128 * 1024 * 1024,
            "pids": 8,
            "max_concurrent": 2,
            "max_model_calls": 20,
            "max_tokens": 50_000,
        }
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "required_syscalls": list(self.required_syscalls),
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "resource_quota": dict(self.resource_quota),
        }


class ScopedAppClient(CerebrumClient):
    """
    Capability-restricted SDK client bound to an installed Agent App's Manifest.
    Enforces that the App can only invoke syscall families and tools declared in its manifest.
    """

    def __init__(self, manifest: AgentManifest, kernel: Any, default_user_id: Optional[str] = None):
        super().__init__(local_executor=kernel.execute_request, default_user_id=default_user_id)
        self.manifest = manifest
        self.kernel = kernel

    def query(self, agent_name: str, query_obj: Any, user_id: Optional[str] = None) -> Dict[str, Any]:
        q_class = getattr(query_obj, "query_class", "unknown")
        if q_class not in self.manifest.required_syscalls:
            raise PermissionError(
                f"Agent App '{self.manifest.app_id}' manifest does not permit '{q_class}' syscalls"
            )
        if isinstance(query_obj, ToolQuery) and self.manifest.allowed_tools is not None:
            for tc in query_obj.tool_calls:
                t_name = tc.get("name")
                if t_name not in self.manifest.allowed_tools:
                    raise PermissionError(
                        f"Agent App '{self.manifest.app_id}' manifest does not permit tool '{t_name}'"
                    )
        return super().query(self.manifest.app_id, query_obj, user_id=user_id)


@dataclass
class AgentPackage:
    manifest: AgentManifest
    entry_fn: Callable[[ScopedAppClient, Dict[str, Any]], Any]


class AgentHub:
    """
    Cerebrum Agent Hub (Development, Deployment, Distribution, and Discovery Platform).
    Allows developers to publish Agent Apps and users to discover and run them on the AIOS Kernel.
    """

    def __init__(self):
        self._apps: Dict[str, Dict[str, AgentPackage]] = {}

    def publish(
        self,
        manifest: AgentManifest,
        entry_fn: Callable[[ScopedAppClient, Dict[str, Any]], Any],
    ) -> Dict[str, str]:
        versions = self._apps.setdefault(manifest.app_id, {})
        versions[manifest.version] = AgentPackage(manifest=manifest, entry_fn=entry_fn)
        return {"app_id": manifest.app_id, "version": manifest.version, "status": "published"}

    def discover(self, keyword: str = "") -> List[Dict[str, Any]]:
        kw = (keyword or "").lower().strip()
        results: List[Dict[str, Any]] = []
        for app_id, versions in self._apps.items():
            latest_ver = sorted(versions.keys())[-1]
            pkg = versions[latest_ver]
            hay = f"{pkg.manifest.app_id} {pkg.manifest.name} {pkg.manifest.description}".lower()
            if not kw or kw in hay:
                results.append(pkg.manifest.to_dict())
        return results

    def load(self, app_id: str, version: Optional[str] = None) -> AgentPackage:
        if app_id not in self._apps:
            raise KeyError(f"Agent App '{app_id}' not found in AgentHub")
        versions = self._apps[app_id]
        target_ver = version or sorted(versions.keys())[-1]
        if target_ver not in versions:
            raise KeyError(f"Version '{target_ver}' of Agent App '{app_id}' not found")
        return versions[target_ver]

    def run_app(
        self,
        app_id: str,
        kernel: Any,
        task_input: Dict[str, Any],
        version: Optional[str] = None,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Install/load the Agent App from the Hub, provision its OS AgentControlBlock quota
        in the AIOS Kernel DRF controller, and execute it inside a manifest-enforced sandbox client.
        """
        pkg = self.load(app_id, version=version)
        quota = pkg.manifest.resource_quota
        drf = kernel.scheduler.drf_controller
        drf.register_agent(
            pkg.manifest.app_id,
            max_concurrent=int(quota.get("max_concurrent", 2)),
            max_model_calls=int(quota.get("max_model_calls", 20)),
            max_tokens=int(quota.get("max_tokens", 50_000)),
            resource_limit=ResourceVector(**{key: quota[key] for key in ("cpu_milli", "memory_bytes", "pids") if key in quota}),
        )
        scoped_client = ScopedAppClient(pkg.manifest, kernel=kernel, default_user_id=user_id)
        output = pkg.entry_fn(scoped_client, task_input)
        return {
            "app_id": pkg.manifest.app_id,
            "version": pkg.manifest.version,
            "output": output,
            "acb_snapshot": drf.snapshot()["agents"].get(pkg.manifest.app_id),
        }
