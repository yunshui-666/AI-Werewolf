"""逐座位模型配置与客户端生命周期。

providers.json 仅保存名称、模型、接口地址及环境变量名。密钥从本地 .env、
进程环境或网页本次提交的内存值读取，任何公开配置响应都只返回 has_key。
"""

import json
import os
import re
from pathlib import Path
from threading import RLock
from uuid import uuid4

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.player import MockAgent, PlayerAgent
from config import LLMConfig
from llm.client import DeepSeekClient, LLMPlayerAgent, RequestPacer


class APIProfile(BaseModel):
    """一个 OpenAI 兼容接口的非敏感元数据。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    profile_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=150)
    key_env: str = Field(default="CUSTOM_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    requires_key: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """只接受完整 HTTP(S) 地址，配置错误在发起请求前报告。"""
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("接口地址需要有效 HTTP(S) URL")
        return value.rstrip("/")


class ProfileRegistry:
    """本地配置注册表；锁只保护短时配置读写，不包围网络调用。"""

    def __init__(self, project_root: Path) -> None:
        """读取非敏感接口列表，并准备独立的内存密钥表。默认 DeepSeek 元数据兼容既有 .env 设置。"""
        self.root = project_root
        self.path = project_root / "providers.json"
        self.lock = RLock()
        self.runtime_keys: dict[str, str] = {}
        self.saved_profiles: dict[str, APIProfile] = {}
        if self.path.exists():
            for item in json.loads(self.path.read_text(encoding="utf-8-sig")):
                profile = APIProfile.model_validate(item)
                if profile.profile_id in {"mock", "auto"}:
                    raise ValueError("mock 和 auto 是保留配置名称")
                self.saved_profiles[profile.profile_id] = profile
        self._refresh()

    def _refresh(self) -> dict:
        """重新发现 .env 接口，下一局即可使用用户新补填的配置。调用方持有 registry 锁。"""
        values = self._env_values()
        self.profiles = {
            "deepseek": APIProfile(
                profile_id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com",
                model="deepseek-flash", key_env="DEEPSEEK_API_KEY",
            ),
            **self.saved_profiles,
        }
        self.warnings: list[str] = []
        # 只发现完整的命名组，避免将其他用途的环境变量误认为游戏模型。
        prefixes = sorted({
            match.group(1) for key in values
            if (match := re.fullmatch(r"([A-Z][A-Z0-9_]*)_(?:API_KEY|BASE_URL|MODEL)", key))
            and values.get(key)
        })
        for prefix in prefixes:
            profile_id = prefix.lower()
            if profile_id in {"mock", "auto", "llm"}:
                continue
            if profile_id in self.saved_profiles:
                continue
            base = values.get(f"{prefix}_BASE_URL")
            model = values.get(f"{prefix}_MODEL")
            if prefix == "DEEPSEEK":
                base = base or "https://api.deepseek.com"
                model = model or "deepseek-flash"
            if not base or not model:
                if base or model:
                    self.warnings.append(f"{prefix} 需要同时填写 {prefix}_BASE_URL 和 {prefix}_MODEL")
                continue
            try:
                self.profiles[profile_id] = APIProfile(
                    profile_id=profile_id, name="DeepSeek" if prefix == "DEEPSEEK" else prefix,
                    base_url=base, model=model, key_env=f"{prefix}_API_KEY",
                )
            except ValueError:
                self.profiles.pop(profile_id, None)
                self.warnings.append(f"{prefix} 的接口地址或模型配置无效")
        # 兼容用户已填写的 DEEPSEEK_API2、API3 等简写，共用主配置的 URL 与模型。
        common = self.profiles.get("deepseek")
        if common is not None:
            numbered = sorted(
                (int(match.group(1)), key_env)
                for key_env, key in values.items()
                if (match := re.fullmatch(r"DEEPSEEK_API([1-9][0-9]*)", key_env))
                and key and key.strip()
            )
            for number, key_env in numbered:
                profile_id = f"deepseek_{number}"
                if profile_id not in self.profiles:
                    self.profiles[profile_id] = APIProfile(
                        profile_id=profile_id, name=f"DeepSeek · API {number}",
                        base_url=common.base_url, model=common.model, key_env=key_env,
                    )
        return values

    def _deepseek_pool(self, values: dict) -> list[str]:
        """按数字排序可用 DeepSeek 配置，保证 API2 排在 API10 前面，空 Key 自动跳过。"""
        candidates = [
            profile_id for profile_id, profile in self.profiles.items()
            if (profile_id == "deepseek" or re.fullmatch(r"deepseek_[1-9][0-9]*", profile_id))
            and self._ready(profile, values)
        ]
        return sorted(candidates, key=lambda name: (1 if name == "deepseek" else int(name.split("_")[-1]), name))

    def _ready(self, profile: APIProfile, values: dict) -> bool:
        """就绪状态只说明凭据已填写，服务可用性由实际请求确认。"""
        return not profile.requires_key or bool(
            (self.runtime_keys.get(profile.profile_id) or values.get(profile.key_env) or "").strip()
        )

    def resolve_selections(self, player_count: int, selections: dict[int, str]) -> dict[int, str]:
        """auto 按 DeepSeek Key 循环分配。真人和显式指定的座位独立处理，不占循环名额。"""
        with self.lock:
            values = self._refresh()
            ready = [pid for pid, profile in self.profiles.items() if self._ready(profile, values)]
            default = (values.get("LLM_DEFAULT_PROVIDER") or "").strip()
            pool = self._deepseek_pool(values)
            # 主 Key 留空、编号 Key 可用时，自动模式同样可以使用编号配置。
            default = default or ("deepseek" if pool else ready[0] if ready else "mock")
            if default not in self.profiles and default.lower() in self.profiles:
                default = default.lower()
            pool_index = 0
            resolved = {}
            for pid in range(1, player_count + 1):
                selected = selections.get(pid, "auto").strip()
                if selected == "auto":
                    seat_override = (values.get(f"AGENT_{pid}_PROVIDER") or "").strip()
                    if seat_override:
                        selected = seat_override
                    elif default == "deepseek" and pool:
                        selected = pool[pool_index % len(pool)]
                        pool_index += 1
                    else:
                        selected = default
                if selected not in self.profiles and selected.lower() in self.profiles:
                    selected = selected.lower()
                if selected != "mock":
                    profile = self.profiles.get(selected)
                    if profile is None:
                        raise ValueError(f"玩家 {pid} 的接口配置不存在，请检查 PROVIDER 与命名组")
                    if not self._ready(profile, values):
                        raise ValueError(f"玩家 {pid} 的接口尚未配置 API Key")
                resolved[pid] = selected
            return resolved

    def routing_setup(
        self, player_count: int = 12, human_id: int = 1, selections: dict[int, str] | None = None,
    ) -> dict:
        """预览与真实开局复用同一套分配，跳过真人，并考虑网页已选择的固定接口。"""
        with self.lock:
            requested = dict(selections or {})
            if human_id:
                requested[human_id] = "mock"
            errors = []
            try:
                defaults = {str(pid): profile for pid, profile in self.resolve_selections(player_count, requested).items()}
            except ValueError as exc:
                defaults = {str(pid): None for pid in range(1, player_count + 1)}
                errors.append(str(exc))
            values = self._env_values()
            return {
                "seat_defaults": defaults, "config_warnings": list(self.warnings) + errors,
                "deepseek_pool": self._deepseek_pool(values),
            }

    def _env_values(self) -> dict:
        """每次取用时读取本地配置，让用户补填 Key 后能够立即使用。"""
        return {**dotenv_values(self.root / ".env", encoding="utf-8-sig"), **os.environ}

    def public_profiles(self) -> list[dict]:
        """返回可给浏览器查看的配置列表，始终省略密钥内容。"""
        with self.lock:
            values = self._refresh()
            result = [{"profile_id": "mock", "name": "离线策略 AI", "model": "MockAgent", "has_key": True}]
            for profile in self.profiles.values():
                result.append({
                    **profile.model_dump(),
                    "has_key": self._ready(profile, values),
                })
            return result

    def save(self, profile: APIProfile, api_key: str = "") -> None:
        """原子替换非敏感 JSON，网页输入的 Key 仅进入当前服务的内存。"""
        if profile.profile_id in {"mock", "auto"}:
            raise ValueError("mock 和 auto 是保留配置名称")
        with self.lock:
            updated = {**self.saved_profiles, profile.profile_id: profile}
            temporary = self.path.with_name(f".providers-{uuid4().hex}.tmp")
            try:
                temporary.write_text(
                    json.dumps([item.model_dump() for item in updated.values()], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self.saved_profiles = updated
            self._refresh()
            if api_key.strip():
                self.runtime_keys[profile.profile_id] = api_key.strip()

    def create_agents(
        self, player_count: int, selections: dict[int, str], seed: int | None,
    ) -> tuple[dict[int, PlayerAgent], list[DeepSeekClient]]:
        """每位玩家独立 Agent，同一接口共用无对话状态的 SDK 连接池。"""
        agents: dict[int, PlayerAgent] = {}
        clients: dict[str, DeepSeekClient] = {}
        pacer = RequestPacer()
        try:
            resolved = self.resolve_selections(player_count, selections)
            for pid, selected in resolved.items():
                if selected == "mock":
                    agents[pid] = MockAgent(None if seed is None else seed * 100 + pid)
                    agents[pid].profile_id = "mock"
                    agents[pid].profile_name = "离线策略 AI"
                    continue
                with self.lock:
                    profile = self.profiles.get(selected)
                    if profile is None:
                        raise ValueError("选中的接口配置不存在")
                    key = (self.runtime_keys.get(selected) or self._env_values().get(profile.key_env) or "").strip()
                    if not key and profile.requires_key:
                        raise ValueError("选中的接口尚未配置 API Key")
                    config = LLMConfig.from_values(
                        self._env_values(), api_key=key or "local-no-key",
                        base_url=profile.base_url, model=profile.model,
                        prefix=(
                            "DEEPSEEK" if re.fullmatch(r"DEEPSEEK_API[1-9][0-9]*", profile.key_env)
                            else profile.key_env.removesuffix("_API_KEY")
                        ),
                    )
                if selected not in clients:
                    clients[selected] = DeepSeekClient(config, pacer=pacer)
                agents[pid] = LLMPlayerAgent(clients[selected])
                agents[pid].profile_id = selected
                agents[pid].profile_name = f"{profile.name} · {profile.model}"
            return agents, list(clients.values())
        except Exception:
            for client in clients.values():
                client.close()
            raise
