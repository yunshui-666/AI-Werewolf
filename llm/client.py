"""通过 OpenAI 兼容 SDK 调用 DeepSeek，并限制重试与日志内容。"""

from contextlib import contextmanager
import logging
from threading import Lock
import time
from urllib.parse import urlsplit

from openai import APIError, APIStatusError, APITimeoutError, OpenAI
from pydantic import ValidationError

from agents.player import Action, Decision, PlayerAgent
from agents.prompts import build_messages
from agents.schemas import DecisionOutput
from config import LLMConfig
from game.state import PlayerView

logger = logging.getLogger("werewolf_arena.public")
admin_logger = logging.getLogger("werewolf_arena.admin")


class LLMUnavailable(RuntimeError):
    """本次决策失败，主持人可采用本地合法降级行动。"""


class RequestPacer:
    """一局共用的请求节拍器，上次请求结束后至少休息 1 秒，再允许下一次请求。"""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.lock = Lock()
        self.next_request_at = 0.0

    @contextmanager
    def slot(self):
        """锁覆盖网络调用，确保不同座位、不同接口和重试都遵守同一条串行时间线。"""
        with self.lock:
            delay = self.next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                yield
            finally:
                # 失败的请求也占用一次会话，随后同样休息，避免异常触发请求风暴。
                self.next_request_at = time.monotonic() + self.interval


class DeepSeekClient:
    """OpenAI 兼容的无对话状态客户端。每次请求重建个人上下文，并对超时、服务错误和坏输出进行有界重试。"""
    def __init__(self, config: LLMConfig, sdk_client: OpenAI | None = None, *, pacer: RequestPacer | None = None) -> None:
        """保存接口配置并准备无共享聊天记录的 SDK 连接，重试计数属于当前接口实例。"""
        self.config = config
        self.pacer = pacer if pacer is not None else RequestPacer()
        self.sdk = sdk_client if sdk_client is not None else OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=0,
        )
        self.disabled = False
        self.request_count = 0
        self.total_tokens = 0
        self.failed_decisions = 0
        # DeepSeek 当前模型默认深度思考，会占用输出预算且使温度参数失效。
        # 只为已确认协议的官方模型自动关闭，其他接口保留其自身默认行为。
        thinking = config.thinking
        if thinking == "auto" and urlsplit(config.base_url).hostname == "api.deepseek.com" and config.model in {"deepseek-flash", "deepseek-v4-pro"}:
            thinking = "disabled"
        self.request_options = {} if thinking == "auto" else {"extra_body": {"thinking": {"type": thinking}}}

    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """根据本人视图、当前动作和合法候选生成一次决策。输出只携带行动建议，真实状态由主持人更新。"""
        if self.disabled:
            raise LLMUnavailable("模型服务已暂停，本局使用本地降级")
        messages = build_messages(view, action, candidates)
        for attempt in range(self.config.max_retries + 1):
            failure_reason = "模型输出为空或生成中断"
            try:
                self.request_count += 1
                with self.pacer.slot():
                    response = self.sdk.chat.completions.create(
                        model=self.config.model,
                        messages=messages,
                        response_format={"type": "json_object"},
                        max_tokens=self.config.max_tokens,
                        temperature=self.config.temperature,
                        stream=False,
                        **self.request_options,
                    )
                if response.usage is not None:
                    self.total_tokens += response.usage.total_tokens
                choice = response.choices[0]
                if choice.finish_reason != "stop" or not choice.message.content:
                    raise ValueError("模型输出为空或尚未完整生成")
                output = DecisionOutput.model_validate_json(
                    choice.message.content,
                    context={
                        "action": action,
                        "candidates": candidates,
                        "view": view,
                        "suspect_ids": tuple(p.player_id for p in view.players if p.alive and p.player_id != view.player_id),
                    },
                )
                self.failed_decisions = 0
                return output.to_decision()
            except APIStatusError as exc:
                description = {
                    400: "请求参数或 JSON 模式不兼容", 401: "API Key 无效或已失效",
                    402: "账户余额或计费状态异常", 403: "接口或模型访问权限不足",
                    404: "模型名称或接口路径不存在", 408: "服务端请求超时",
                    409: "服务端请求冲突", 429: "请求频率或用量达到限制",
                }.get(exc.status_code, "模型服务暂时异常")
                failure_reason = f"HTTP {exc.status_code}，{description}"
                if exc.status_code in {401, 402, 403, 404}:
                    self.disabled = True
                if exc.status_code not in {408, 409, 429} and exc.status_code < 500:
                    logger.warning("模型调用失败（%s）。", failure_reason)
                    self._record_failure()
                    raise LLMUnavailable(failure_reason) from None
                error_type = type(exc).__name__
            except ValidationError as exc:
                error_type = type(exc).__name__
                failure_reason = "模型返回的 JSON 格式错误" if any(item["type"] == "json_invalid" for item in exc.errors()) else "输出字段或行动目标未通过校验"
                # 纠错信息不回显模型原文和校验输入，仍只使用当前玩家已有信息。
                messages = messages[:2] + [{
                    "role": "user",
                    "content": (
                        "上次输出未通过结构或行动校验。请重新输出本次行动的 JSON，"
                        "顶层严格使用 target_id、text、suspicions、potion 四个字段。"
                        "按当前 action、legal_target_ids 与本人技能资源生成合法行动，"
                        "公开发言和狼人私聊需要非空 text，suspicions 只记录其他存活玩家。"
                    ),
                }]
            except (APIError, ValueError, IndexError, TypeError) as exc:
                error_type = type(exc).__name__
                if isinstance(exc, APITimeoutError):
                    failure_reason = "网络或模型响应超时"
                elif isinstance(exc, APIError):
                    failure_reason = "网络连接或模型服务异常"
            if attempt < self.config.max_retries:
                logger.warning("一次模型调用需要重试（%s，%s，第 %s 次）。", error_type, failure_reason, attempt + 1)
                admin_logger.debug("玩家 %s 的 %s 调用重试（%s）。", view.player_id, action.value, error_type)
                time.sleep(min(2 ** attempt, 4))
        logger.warning("模型调用达到重试上限（%s，%s）。", error_type, failure_reason)
        self._record_failure()
        raise LLMUnavailable("达到模型调用重试上限")

    def _record_failure(self) -> None:
        """累计当前接口连续失败次数，达到三次后停止本局后续远程请求，交由本地策略合法降级。"""
        self.failed_decisions += 1
        if self.failed_decisions >= 3:
            self.disabled = True

    def close(self) -> None:
        """释放本局 SDK 连接资源。共享同一配置的多个 Agent 由创建者统一关闭一次客户端。"""
        self.sdk.close()


class LLMPlayerAgent(PlayerAgent):
    """将统一 PlayerAgent 接口委托给指定 API 客户端，客户端不会替玩家持有共享聊天历史。"""

    decision_source = "api"
    def __init__(self, client: DeepSeekClient) -> None:
        """保存已创建的 API 客户端引用，当前玩家的对话与记忆仍由每次传入的个人快照提供。"""
        self.client = client

    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """根据本人视图、当前动作和合法候选生成一次决策。输出只携带行动建议，真实状态由主持人更新。"""
        return self.client.decide(view, action, candidates)
