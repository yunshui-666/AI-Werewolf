"""游戏与模型配置。环境变量优先于项目 .env 文件。"""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path

# 程序接口保留 classic8，桌面命令行与网页默认选择 standard12。
# 配置只描述角色数量，随机分配与技能执行仍由规则引擎负责。
PRESETS = {
    "classic8": {"werewolf": 2, "seer": 1, "villager": 5},
    "standard12": {"werewolf": 4, "seer": 1, "witch": 1, "hunter": 1, "guard": 1, "villager": 4},
}


@dataclass(frozen=True)
class GameConfig:
    """一局的固定参数；人数从预设计算，避免各模块分别硬编码座位数。"""
    max_rounds: int = 10
    seed: int | None = None
    discussion_rounds: int = 1
    preset: str = "classic8"

    def __post_init__(self) -> None:
        """在创建配置对象时验证参数边界，错误尽早在启动阶段报告，避免运行中才发现无效设置。"""
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int):
            raise ValueError("最大轮数必须为正整数")
        if self.max_rounds < 1:
            raise ValueError("最大轮数必须为正整数")
        if type(self.discussion_rounds) is not int or self.discussion_rounds < 1:
            raise ValueError("每日发言轮数必须为正整数")
        if self.preset not in PRESETS:
            raise ValueError("未知游戏配置")

    @property
    def player_count(self) -> int:
        """界面、Agent 创建与 ID 校验共用的人数来源。"""
        return sum(PRESETS[self.preset].values())


@dataclass(frozen=True)
class LLMConfig:
    """模型连接参数。api_key 从 repr 中隐藏，环境变量优先于本地 .env。"""
    api_key: str = field(repr=False, default="")
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout: float = 60.0
    max_retries: int = 2
    max_tokens: int = 2048
    temperature: float = 0.9
    thinking: str = "auto"

    def __post_init__(self) -> None:
        """在创建配置对象时验证参数边界，错误尽早在启动阶段报告，避免运行中才发现无效设置。"""
        if not self.api_key.strip():
            raise ValueError("请在项目 .env 或环境变量中填写所选接口的 API Key")
        if not self.base_url.startswith(("http://", "https://")) or not self.model.strip():
            raise ValueError("模型接口 URL 或模型名称配置无效")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("LLM_TIMEOUT 必须为有限正数")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("LLM_MAX_RETRIES 必须为非负整数")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("LLM_MAX_TOKENS 必须为正整数")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("LLM_TEMPERATURE 必须在 0 到 2 之间")
        if self.thinking not in {"auto", "enabled", "disabled"}:
            raise ValueError("THINKING 必须为 auto、enabled 或 disabled")

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "LLMConfig":
        """按项目路径读取 .env 并由进程环境覆盖，数值转换失败时返回固定提示，避免把原始配置内容带入日志。"""
        from dotenv import dotenv_values

        path = env_file if env_file is not None else Path(__file__).resolve().parent / ".env"
        values = {**dotenv_values(path, encoding="utf-8-sig"), **os.environ}
        return cls.from_values(
            values, api_key=values.get("DEEPSEEK_API_KEY") or "",
            base_url=values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            model=values.get("DEEPSEEK_MODEL") or "deepseek-flash", prefix="DEEPSEEK",
        )

    @classmethod
    def from_values(
        cls, values: dict, *, api_key: str, base_url: str, model: str, prefix: str = "",
    ) -> "LLMConfig":
        """所有接口共用数值配置解析。接口前缀参数优先，其次 LLM_*，最后使用默认值。"""
        def parameter(name: str, default: str) -> str:
            return values.get(f"{prefix}_{name}") or values.get(f"LLM_{name}") or default

        try:
            return cls(
                api_key=api_key.strip(), base_url=base_url.strip(), model=model.strip(),
                timeout=float(parameter("TIMEOUT", "60")),
                max_retries=int(parameter("MAX_RETRIES", "2")),
                max_tokens=int(parameter("MAX_TOKENS", "2048")),
                temperature=float(parameter("TEMPERATURE", "0.9")),
                thinking=parameter("THINKING", "auto").strip().lower(),
            )
        except (TypeError, AttributeError, ValueError):
            # 数值解析错误可能带出输入内容，向 CLI 返回固定提示。
            raise ValueError("模型配置无效，请检查 API Key、URL、模型和数值参数") from None
