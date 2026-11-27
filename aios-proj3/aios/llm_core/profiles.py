"""
RAM-only Multi-API Credential & Profile Broker for AIOS LLM Core.
Keys are held exclusively in memory, never written to disk, and never returned in status snapshots.
"""
import re
import threading
from typing import Any, Dict, List
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass
from aios.llm_core.streaming import ModelStreamError

PROVIDER_ENDPOINTS = {
    "deepseek": ("https://api.deepseek.com/v1", "https://api.deepseek.com"),
    "openai": ("https://api.openai.com/v1",),
    "aizex": ("https://ca.memofun.net/v1", "https://codex-api.aizex.net/v1"),
    "ollama": ("http://127.0.0.1:11434/v1",),
    "vllm": ("http://127.0.0.1:8000/v1",),
}


DEEPSEEK_MODEL = "deepseek-flash"
OLD_DEEPSEEK_NAMES = {"deepseek-chat", "deepseek-reasoner", "deepseek_chat", "deepseek_reasoner"}


def model_suggestion(provider, model):
    return DEEPSEEK_MODEL if provider == "deepseek" and model in OLD_DEEPSEEK_NAMES else None


def connection_route(endpoint):
    if not endpoint:
        return None
    url = urlsplit(endpoint)
    return "proxy" if getproxies().get(url.scheme) and not proxy_bypass(url.hostname) else "direct"


def provider_endpoint(provider: str, value: str = "") -> str:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid provider endpoint")
    allowed = PROVIDER_ENDPOINTS[provider]
    endpoint = value.rstrip("/") or allowed[0]
    if provider in ("ollama", "vllm"):
        url = urlsplit(endpoint)
        if (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1") and url.port
                and not url.username and not url.password and url.path == "/v1"
                and not url.query and not url.fragment):
            return endpoint
    if endpoint not in allowed:
        raise ValueError("Endpoint does not belong to the configured provider")
    return endpoint


class ModelProfileBroker:
    """
    Isolated RAM-only credential & provider profile manager (ported from v0.21-v0.25 MultiModelBroker).
    Supports up to 8 profiles across `deepseek`, `openai`, `aizex`, `ollama`, and `vllm`.
    """

    VALID_PROVIDERS = ("deepseek", "openai", "aizex", "ollama", "vllm")

    def __init__(self, legacy_model: str = DEEPSEEK_MODEL):
        self.lock = threading.RLock()
        self.legacy_model = legacy_model
        self._legacy_key: str = ""
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.selected: str = "legacy"
        self.active_calls: int = 0
        self.connection_checks = {}

    def _ensure_idle(self) -> None:
        if self.active_calls > 0:
            raise ModelStreamError("Wait for in-flight model calls to finish before changing profiles")

    @staticmethod
    def validate_key(key: Any) -> None:
        if not isinstance(key, str) or not 16 <= len(key) <= 512 or any(c.isspace() for c in key):
            raise ValueError("Invalid supplier key format")

    def configure_legacy(self, key: str) -> Dict[str, Any]:
        if key:
            self.validate_key(key)
        with self.lock:
            self._ensure_idle()
            if key != self._legacy_key:
                self.connection_checks.pop("legacy", None)
            self._legacy_key = key
            if key:
                self.selected = "legacy"
            return {"configured": bool(key)}

    def save_profile(self, value: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, dict) or set(value) - {"id", "label", "provider", "model", "key", "base_url"}:
            raise ValueError("Invalid profile fields")
        identity = value.get("id")
        label = value.get("label")
        provider = value.get("provider")
        model = value.get("model")
        base_url = value.get("base_url", "")

        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", identity)
            or identity == "legacy"
            or not isinstance(label, str)
            or not 1 <= len(label) <= 60
            or any(ord(c) < 32 for c in label)
            or provider not in self.VALID_PROVIDERS
            or not isinstance(model, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model)
            or "sk-" in label.lower()
            or "sk-" in model.lower()
        ):
            raise ValueError("Invalid profile metadata")
        base_url = provider_endpoint(provider, base_url)

        with self.lock:
            self._ensure_idle()
            old = self.profiles.get(identity)
            if old and old["provider"] != provider:
                raise ValueError("Provider is immutable; create a separate profile with its own key")
            if old is None and len(self.profiles) >= 7:
                raise ValueError("At most eight API profiles including legacy")

            key = value.get("key", old["key"] if old else None)
            self.validate_key(key)

            all_secrets = [k for k in [key, self._legacy_key] + [p["key"] for p in self.profiles.values()] if k]
            if any(sec in label or sec in model for sec in all_secrets):
                raise ValueError("Profile metadata cannot contain credentials")

            revision = (
                old["revision"]
                + int(
                    any(
                        old[k] != v
                        for k, v in {"label": label, "provider": provider, "model": model,
                                     "key": key, "base_url": base_url}.items()
                    )
                )
                if old
                else 1
            )
            if not old or revision != old["revision"]:
                self.connection_checks.pop(identity, None)
            self.profiles[identity] = {
                "id": identity,
                "label": label,
                "provider": provider,
                "model": model,
                "key": key,
                "base_url": base_url,
                "revision": revision,
            }
            return {"id": identity, "configured": True, "selected": self.selected == identity, "revision": revision}

    def save_and_select(self, value):
        with self.lock:
            profile = self.save_profile(value)
            return self.select_profile(profile["id"])

    def model_config(self, profile_id=None):
        """Take a private immutable snapshot of a configured profile for one call."""
        with self.lock:
            profile_id = self.selected if profile_id is None else profile_id
            if profile_id == "legacy" and self._legacy_key:
                return {"profile_id": "legacy", "name": self.legacy_model, "backend": "deepseek",
                        "hostname": PROVIDER_ENDPOINTS["deepseek"][0], "api_key": self._legacy_key}
            profile = self.profiles.get(profile_id)
            if profile:
                provider = profile["provider"]
                return {"profile_id": profile["id"], "revision": profile["revision"], "name": profile["model"],
                        "backend": provider, "hostname": profile["base_url"], "api_key": profile["key"],
                        "protocol": "responses" if provider == "aizex" else "chat_completions"}
            return None

    def record_connection(self, config, result):
        with self.lock:
            profile_id = config["profile_id"]
            if self.model_config(profile_id) == config:
                self.connection_checks[profile_id] = dict(result)

    def _public_status(self, identity, provider, model, endpoint):
        return {"base_url": endpoint, "transport": connection_route(endpoint),
                "suggested_model": model_suggestion(provider, model),
                "connection": self.connection_checks.get(identity)}

    def select_profile(self, profile_id: str) -> Dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            if profile_id == "legacy":
                ready = bool(self._legacy_key)
            else:
                ready = profile_id in self.profiles
            if not ready:
                raise ValueError("Select a configured API profile")
            self.selected = profile_id
            return self.status()

    def remove_profile(self, profile_id: str) -> Dict[str, Any]:
        if profile_id == "legacy":
            raise ValueError("Cannot remove legacy profile slot")
        with self.lock:
            self._ensure_idle()
            if profile_id not in self.profiles:
                raise ValueError("Unknown profile")
            self.profiles.pop(profile_id)
            self.connection_checks.pop(profile_id, None)
            if self.selected == profile_id:
                self.selected = "legacy" if self._legacy_key else ""
            return {"removed": True, "selected": self.selected}

    def profiles_info(self) -> Dict[str, Any]:
        with self.lock:
            rows: List[Dict[str, Any]] = [
                {
                    "id": "legacy",
                    "label": "DeepSeek 默认槽位",
                    "provider": "deepseek",
                    "model": self.legacy_model,
                    "configured": bool(self._legacy_key),
                    "legacy": True,
                    "revision": None,
                    **self._public_status("legacy", "deepseek", self.legacy_model, PROVIDER_ENDPOINTS["deepseek"][0]),
                }
            ]
            for p in self.profiles.values():
                rows.append(
                    {
                        "id": p["id"],
                        "label": p["label"],
                        "provider": p["provider"],
                        "model": p["model"],
                        "configured": bool(p["key"]),
                        "legacy": False,
                        "revision": p["revision"],
                        **self._public_status(p["id"], p["provider"], p["model"], p["base_url"]),
                    }
                )
            return {
                "selected": self.selected,
                "profiles": rows,
                "max_profiles": 8,
                "key_storage": "memory",
                "automatic_failover": False,
            }

    def status(self) -> Dict[str, Any]:
        with self.lock:
            info = self.profiles_info()
            selected = next((p for p in info["profiles"] if p["id"] == self.selected), None)
            return {
                "configured": bool(selected and selected["configured"]),
                "model": selected["model"] if selected else "unselected",
                "provider": selected["provider"] if selected else None,
                "profile_label": selected["label"] if selected else None,
                "endpoint": selected["base_url"] if selected else None,
                "transport": selected["transport"] if selected else None,
                "connection": selected["connection"] if selected else None,
                "suggested_model": selected["suggested_model"] if selected else None,
                "profile_id": self.selected,
                "profile_revision": selected["revision"] if selected else None,
                "profiles_supported": True,
                "busy": self.active_calls,
            }
