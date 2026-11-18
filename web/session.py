"""网页游戏会话与可暂停的真人 Agent。

LangGraph 在后台线程顺序执行。轮到真人时，WebHumanAgent 在 Condition 上
等待；HTTP 线程仍可读取白名单快照和提交行动。等待期间释放锁，避免死锁。
"""

from dataclasses import asdict
from threading import Condition, Event, Thread
from uuid import uuid4
import logging

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.player import Action, Decision, PlayerAgent
from agents.schemas import DecisionOutput
from config import GameConfig
from game.engine import ROLE_NAMES
from game.reporting import final_summary
from game.runner import GameRunner
from game.state import Phase, PlayerView, Role
from llm.profiles import ProfileRegistry
from workflow.graph import run_graph


class SessionWarningHandler(logging.Handler):
    """将脱敏后的接口警告写入当前对局日志，便于区分限流、超时和结构校验问题。"""

    def __init__(self, target: logging.Logger) -> None:
        super().__init__(level=logging.WARNING)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        self.target.handle(record)


class SessionOptions(BaseModel):
    """浏览器开局参数，人数由服务端板子决定。0 号代表全 AI 观战。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    preset: str = "standard12"
    human_id: int = Field(default=1, ge=0, le=12)
    seed: int | None = None
    discussion_rounds: int = Field(default=2, ge=1, le=10)
    max_rounds: int = Field(default=10, ge=1, le=100)
    delay_ms: int = Field(default=1000, ge=0, le=5000)
    selections: dict[str, str] = Field(default_factory=dict)
    # 普通 Python/HTTP 调用保留自动运行，网页开局显式开启播放确认。
    playback_sync: bool = False

    @model_validator(mode="after")
    def check_seats(self):
        """同时校验板子和每个座位，拒绝把 12 人配置误用于 8 人局。"""
        config = self.game_config()
        if self.human_id > config.player_count:
            raise ValueError("真人座位超出本局人数")
        if any(not key.isdigit() or not 1 <= int(key) <= config.player_count for key in self.selections):
            raise ValueError("API 配置含无效座位")
        return self

    def game_config(self) -> GameConfig:
        """将 HTTP 配置转换为引擎已有的纯 Python 配置。"""
        return GameConfig(
            preset=self.preset, seed=self.seed,
            discussion_rounds=self.discussion_rounds, max_rounds=self.max_rounds,
        )


class WebHumanAgent(PlayerAgent):
    """只持有自己的最新视图，网页提交必须匹配本次行动的 request_id。"""

    human_controlled = True

    def __init__(self, session: "GameSession") -> None:
        """只保存本局输入协调器，等待请求时使用主持人传入的本人视图。"""
        self.session = session

    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """等待一次合法提交。重复点击、过期提交都不会变成下一次行动。"""
        session = self.session
        with session.condition:
            session.pending = {
                "request_id": uuid4().hex, "action": action.value,
                "player_id": view.player_id, "candidates": list(candidates),
            }
            session.pending_view = view
            session.submitted = None
            session.status = "waiting"
            session.condition.notify_all()
            while session.submitted is None and not session.stopped.is_set():
                session.condition.wait(timeout=1)
            if session.stopped.is_set():
                raise EOFError("本局已停止")
            result = session.submitted
            session.pending = None
            session.pending_view = None
            session.submitted = None
            session.status = "running"
            return result


class ObservedAgent(PlayerAgent):
    """装饰决策接口以提供公开发言高亮和可调节拍，保持实际 Agent 独立。"""

    def __init__(self, agent: PlayerAgent, session: "GameSession") -> None:
        """包装一个真实 Agent，透传真人标志，并提供网页需要的公开发言高亮与节拍。"""
        self.agent = agent
        self.session = session
        self.human_controlled = agent.human_controlled
        self.decision_source = agent.decision_source

    def observe(self, view: PlayerView) -> None:
        """将私有更新交回原 Agent；网页私有面板直接使用本人视图构造。"""
        self.agent.observe(view)

    def bind_role_skill(self, skill) -> None:
        """网页装饰层和实际 Agent 同时绑定本人的专属 skill。"""
        super().bind_role_skill(skill)
        self.agent.bind_role_skill(skill)

    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """夜间不公布行动者编号，只有白天公开发言才高亮对应座位。"""
        with self.session.condition:
            self.session.active_player = view.player_id if action is Action.SPEAK else None
        decision = self.agent.decide(view, action, candidates)
        if action is Action.SPEAK and not self.human_controlled:
            self.session.stopped.wait(self.session.options.delay_ms / 1000)
        return decision


class GameSession:
    """管理一局后台任务、输入请求和浏览器可见的状态。"""

    def __init__(self, options: SessionOptions, registry: ProfileRegistry) -> None:
        """初始化当前对象持有的会话资源。实际游戏状态由独立 GameRunner 管理，输入等待通过该会话协调。"""
        self.options = options
        self.session_id = uuid4().hex
        self.condition = Condition()
        self.stopped = Event()
        self.pending = None
        self.pending_view = None
        self.submitted = None
        self.active_player = None
        self.playback = None
        self.last_playback_id = None
        self.status = "ready"
        self.error = None
        self.log_path = registry.root / "logs" / f"game_{self.session_id[:12]}.log"
        selections = {int(pid): profile for pid, profile in options.selections.items() if int(pid) != options.human_id}
        if options.human_id:
            selections[options.human_id] = "mock"
        agents, self.clients = registry.create_agents(options.game_config().player_count, selections, options.seed)
        self.resolved_profiles = {
            pid: {"profile_id": agent.profile_id, "profile_name": agent.profile_name}
            for pid, agent in agents.items()
        }
        if options.human_id:
            self.resolved_profiles[options.human_id] = {"profile_id": "human", "profile_name": "真人"}
        try:
            if options.human_id:
                agents[options.human_id] = WebHumanAgent(self)
            agents = {pid: ObservedAgent(agent, self) for pid, agent in agents.items()}
            self.runner = GameRunner(
                options.game_config(), agents, stop_requested=self.stopped.is_set,
                after_speech=self.wait_for_playback,
            )
        except Exception:
            for client in self.clients:
                client.close()
            raise
        self.thread = Thread(target=self._run, name=f"werewolf-{self.session_id[:8]}", daemon=True)

    def start(self) -> None:
        """启动一次后台任务。重复启动由 HTTP 会话管理器阻止。"""
        self.status = "running"
        self.thread.start()

    def _run(self) -> None:
        """所有规则写入在同一游戏线程执行，结束时释放该局 SDK 连接。"""
        handler = None
        warning_handler = None
        public_logger = logging.getLogger("werewolf_arena.public")
        logger = logging.Logger(f"werewolf_arena.session.{self.session_id}", level=logging.INFO)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(self.log_path, mode="x", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            warning_handler = SessionWarningHandler(logger)
            public_logger.addHandler(warning_handler)
            for event in self.runner.engine.state.public_events:
                logger.info("%s", event.text)
            def record(event):
                logger.info("%s", event.text)
                if event.decision_source is not None:
                    logger.info("公开发言来源为 %s。", event.decision_source)
                if event.kind == "speech" and self.options.playback_sync and event.actor_id != self.options.human_id:
                    # 在引擎发布事件的同一状态锁范围内建立屏障，避免轮询先看到完整文本。
                    with self.condition:
                        self.playback = {
                            "playback_id": uuid4().hex,
                            "event_index": len(self.runner.engine.state.public_events) - 1,
                            "player_id": event.actor_id,
                            "discussion_round": self.runner.engine.state.discussion_round,
                        }
            self.runner.engine.public_sink = record
            run_graph(self.runner)
            logger.info("%s", final_summary(self.runner.engine, self.runner.fallback_count))
            self.status = "finished"
        except EOFError:
            self.status = "stopped"
        except Exception as exc:
            self.status = "error"
            # 原始异常可能携带 HTTP 请求或 Key，网页只接收固定描述和类型名。
            self.error = f"本局运行中断（{type(exc).__name__}），可重新开局。"
        finally:
            self.runner.engine.public_sink = None
            if warning_handler is not None:
                public_logger.removeHandler(warning_handler)
                warning_handler.close()
            if handler is not None:
                handler.close()
            with self.condition:
                self.pending = None
                self.pending_view = None
                self.playback = None
                self.condition.notify_all()
            for client in self.clients:
                client.close()

    def stop(self) -> None:
        """唤醒正在等待的真人；正在进行的模型请求在自身超时边界返回。"""
        self.stopped.set()
        with self.condition:
            if self.thread.is_alive():
                self.status = "stopping"
            self.condition.notify_all()

    def submit(self, request_id: str, payload: dict) -> None:
        """先用当前候选目标及资源校验，再交给游戏线程执行真正的行动。"""
        with self.condition:
            if not self.pending or self.pending["request_id"] != request_id or self.submitted is not None:
                raise ValueError("这次行动已提交或已经过期，请刷新当前局面")
            action = Action(self.pending["action"])
            view = self.pending_view
            output = DecisionOutput.model_validate(payload, context={
                "action": action, "view": view, "candidates": self.pending["candidates"],
                "suspect_ids": tuple(player.player_id for player in view.players if player.alive and player.player_id != view.player_id),
            })
            self.submitted = output.to_decision()
            self.condition.notify_all()

    def wait_for_playback(self) -> None:
        """等浏览器播放完当前 AI 发言再继续。刷新可从快照恢复，停止会立即唤醒等待。"""
        with self.condition:
            while self.playback is not None and not self.stopped.is_set():
                self.condition.wait(timeout=1)
            if self.stopped.is_set():
                raise EOFError("本局已停止")

    def acknowledge_playback(self, playback_id: str) -> None:
        """确认令牌绑定当前发言，重复确认幂等，过期令牌无法跳过后续玩家。"""
        with self.condition:
            if playback_id and playback_id == self.last_playback_id:
                return
            if not self.playback or self.playback["playback_id"] != playback_id:
                raise ValueError("发言播放确认已过期，请刷新当前局面")
            self.last_playback_id = playback_id
            self.playback = None
            self.condition.notify_all()

    def speech_text(self, event_index: int) -> str:
        """语音服务只接受本局已公开的发言编号，避免提交任意文本或其他玩家的私有信息。"""
        with self.runner.engine.lock:
            events = self.runner.engine.state.public_events
            if type(event_index) is not int or not 0 <= event_index < len(events):
                raise ValueError("发言记录不存在")
            event = events[event_index]
            if event.kind != "speech":
                raise ValueError("只能合成公开发言")
            return event.text.split("：", 1)[-1]

    def snapshot(self) -> dict:
        """构造浏览器白名单响应。绝不对 GameState 进行整体序列化。"""
        engine = self.runner.engine
        with engine.lock, self.condition:
            state = engine.state
            finished = state.phase is Phase.FINISHED
            disclosed = {event.actor_id: Role.HUNTER for event in state.public_events if event.kind == "hunter_shot"}
            own_id = self.options.human_id
            own = engine.player_view(own_id) if own_id else None
            players = []
            for pid, player in sorted(state.players.items()):
                known_role = player.role if finished or pid == own_id else disclosed.get(pid)
                players.append({
                    "player_id": pid, "alive": player.alive,
                    "role": known_role.value if known_role else None,
                    "role_name": ROLE_NAMES[known_role] if known_role else "身份未公开",
                    "human": pid == own_id,
                    **self.resolved_profiles[pid],
                })
            return {
                "session_id": self.session_id, "status": self.status, "error": self.error,
                "preset": state.preset, "round_number": state.round_number,
                "phase": state.phase.value, "discussion_round": state.discussion_round,
                "discussion_rounds": state.discussion_rounds,
                "active_player": self.active_player if state.phase is Phase.DAY and self.status in {"running", "waiting"} else None,
                "outcome": state.outcome.value if state.outcome else None,
                "players": players,
                "events": [asdict(event) for event in state.public_events],
                "playback": dict(self.playback) if self.playback else None,
                "private": asdict(own) if own else None,
                "pending": dict(self.pending) if self.pending and self.submitted is None else None,
                "fallback_count": self.runner.fallback_count if finished else None,
                "model_requests": sum(client.request_count for client in self.clients) if finished else None,
                "log_file": self.log_path.name,
            }
