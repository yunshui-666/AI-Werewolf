"""主持人调用 Agent 后把合法决策交给引擎。节点供顺序执行和 LangGraph 共用。"""

import logging

from agents.player import Action, Decision, MockAgent, PlayerAgent
from config import GameConfig
from game.engine import GameEngine
from game.state import GameState, Phase, Role
from agents.schemas import DecisionOutput
from collections.abc import Callable

logger = logging.getLogger("werewolf_arena.public")
admin_logger = logging.getLogger("werewolf_arena.admin")


class GameRunner:
    """主持人负责调用各 Agent 并把结构化建议交给引擎。Python 顺序流程和 LangGraph 复用这些行动方法。"""
    def __init__(
        self,
        config: GameConfig | None = None,
        agents: dict[int, PlayerAgent] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        after_speech: Callable[[], None] | None = None,
    ) -> None:
        """为每个座位分别创建 Agent 和降级策略，并注册停止信号。角色状态属于 engine，Agent 只持有自己的决策资源。"""
        self.engine = GameEngine(config)
        seed = self.engine.config.seed
        self.agents = agents if agents is not None else {
            pid: MockAgent(None if seed is None else seed * 100 + pid)
            for pid in self.engine.state.players
        }
        if set(self.agents) != set(self.engine.state.players):
            raise ValueError("必须为每位玩家分别配置 Agent")
        for pid, agent in self.agents.items():
            agent.bind_role_skill(self.engine.state.players[pid].role_skill)
        self.fallback_agents = {
            pid: MockAgent(None if seed is None else seed * 100 + pid + 1000)
            for pid in self.engine.state.players
        }
        for pid, agent in self.fallback_agents.items():
            agent.bind_role_skill(self.engine.state.players[pid].role_skill)
        self.fallback_count = 0
        self.last_sources: dict[int, str] = {}
        self.stop_requested = stop_requested or (lambda: False)
        # 网页在这里等待语音/字幕播放确认。回调在状态锁之外运行，HTTP 仍能读取快照。
        self.after_speech = after_speech or (lambda: None)
        self.notify_humans()

    def notify_humans(self) -> None:
        """向真人控制的座位通知其本人视图更新。其他 AI 的私有记忆不经过真人输出通道。"""
        for pid, agent in self.agents.items():
            if agent.human_controlled:
                agent.observe(self.engine.player_view(pid))

    def night_actions(self) -> None:
        """按技能依赖排序。守卫先选目标，女巫在狼刀选好后获得本人刀口。"""
        state = self.engine.state
        for pid in state.alive_ids:
            if state.players[pid].role is Role.GUARD and not state.guard_acted:
                decision = self._decide(pid, Action.GUARD)
                with self.engine.lock:
                    self.engine.guard(pid, decision.target_id)
        for wolf in state.alive_wolf_ids:
            if wolf not in state.wolf_discussions:
                decision = self._decide(wolf, Action.WOLF_DISCUSS)
                with self.engine.lock:
                    self.engine.wolf_discuss(wolf, decision.text)
        for wolf in state.alive_wolf_ids:
            if wolf not in state.wolf_choices:
                self.notify_humans()
                decision = self._decide(wolf, Action.WOLF_KILL)
                with self.engine.lock:
                    self.engine.submit_wolf_choice(wolf, decision.target_id)
        for pid in state.alive_ids:
            if state.players[pid].role is Role.WITCH and state.witch_action is None:
                decision = self._decide(pid, Action.WITCH)
                with self.engine.lock:
                    self.engine.witch(pid, decision.potion, decision.target_id)
        if not state.seer_acted:
            for pid in state.alive_ids:
                if state.players[pid].role is Role.SEER:
                    decision = self._decide(pid, Action.INSPECT)
                    with self.engine.lock:
                        self.engine.inspect(pid, decision.target_id)
        self.notify_humans()

    def resolve_night(self) -> None:
        """夜间死亡后先处理猎人，再让工作流读取最终阶段。"""
        with self.engine.lock:
            self.engine.resolve_night()
        self.resolve_hunters()
        self.notify_humans()

    def resolve_votes(self) -> None:
        """收齐选票后统一公开并结算最高票。平票无人出局，放逐触发的猎人技能会暂缓阶段推进。"""
        with self.engine.lock:
            self.engine.resolve_votes()
        self.resolve_hunters()
        self.notify_humans()

    def resolve_hunters(self) -> None:
        """死亡响应通过相同 Agent 接口，只有引擎授予资格的猎人可行动。"""
        while self.engine.state.pending_hunters:
            pid = self.engine.state.pending_hunters[0]
            decision = self._decide(pid, Action.HUNTER_SHOOT)
            with self.engine.lock:
                self.engine.hunter_shoot(pid, decision.target_id)

    def day_discussion(self) -> None:
        """按存活 ID 顺序执行配置数量的讨论轮次，只有引擎完成最后一轮后才退出 DAY。"""
        while self.engine.state.phase is Phase.DAY:
            for pid in self.engine.state.alive_ids:
                if pid not in self.engine.state.speakers:
                    decision = self._decide(pid, Action.SPEAK)
                    with self.engine.lock:
                        self.engine.speak(pid, decision.text, decision_source=self.last_sources[pid])
                    self.after_speech()

    def voting(self) -> None:
        """对每名尚未投票的存活玩家请求一次决定，选票由引擎暂存，决策时不会把其他待结算选票传出去。"""
        for pid in self.engine.state.alive_ids:
            if pid not in self.engine.state.votes:
                decision = self._decide(pid, Action.VOTE)
                with self.engine.lock:
                    self.engine.cast_vote(pid, decision.target_id)

    def run(self) -> GameState:
        """提供便于学习的 Python 顺序编排。每次结算后检查阶段，夜间终局时直接结束，避免死亡后继续行动。"""
        while self.engine.state.phase is not Phase.FINISHED:
            self.night_actions()
            self.resolve_night()
            if self.engine.state.phase is Phase.FINISHED:
                break
            self.day_discussion()
            self.voting()
            self.resolve_votes()
        return self.engine.state

    def _decide(self, player_id: int, action: Action) -> Decision:
        """决策期间释放状态锁，输入校验与记忆写回在明确边界执行。"""
        if self.stop_requested():
            raise EOFError("游戏已停止")
        with self.engine.lock:
            view = self.engine.player_view(player_id)
        candidates = tuple(
            player.player_id
            for player in view.players
            if player.alive and player.player_id != player_id
        )
        if action in {Action.WOLF_DISCUSS, Action.WOLF_KILL}:
            if view.preset == "standard12":
                candidates = tuple(player.player_id for player in view.players if player.alive)
            else:
                candidates = tuple(pid for pid in candidates if pid not in view.wolf_teammates)
        if action is Action.GUARD:
            candidates = tuple(p.player_id for p in view.players if p.alive and p.player_id != view.previous_guard_target)
        try:
            decision = self.agents[player_id].decide(view, action, candidates)
            if not isinstance(decision, Decision):
                raise ValueError("Agent 返回值类型错误")
            decision = DecisionOutput.model_validate(
                {
                    "target_id": decision.target_id, "text": decision.text, "potion": decision.potion,
                    "suspicions": [{"player_id": pid, "reason": reason} for pid, reason in decision.suspicions],
                },
                context={"action": action, "candidates": candidates, "view": view, "suspect_ids": tuple(p.player_id for p in view.players if p.alive and p.player_id != player_id)},
            ).to_decision()
            with self.engine.lock:
                self.engine.remember_suspicions(player_id, decision.suspicions)
            agent = self.agents[player_id]
            self.last_sources[player_id] = "human" if agent.human_controlled else agent.decision_source
            admin_logger.debug("玩家 %s 动作 %s，决策为 %s。", player_id, action.value, decision)
        except Exception as exc:
            if self.agents[player_id].human_controlled:
                raise
            # 异常消息可能含模型原文、请求或密钥，只记录动作和异常类型。
            logger.warning("一次 AI 决策失败（%s），采用合法降级行动。", type(exc).__name__)
            admin_logger.debug("玩家 %s 的 %s 决策失败（%s）。", player_id, action.value, type(exc).__name__)
            self.fallback_count += 1
            self.last_sources[player_id] = "fallback"
            decision = self.fallback_agents[player_id].decide(view, action, candidates)
            with self.engine.lock:
                self.engine.remember_suspicions(player_id, decision.suspicions)
        if self.stop_requested():
            raise EOFError("游戏已停止")
        return decision
