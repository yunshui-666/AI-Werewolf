"""统一玩家决策接口与基于可见证据的离线博弈策略。"""

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
import random
import re

from agents.memory import Team
from agents.skills import RoleSkill
from agents.strategy import StrategyAnalysis, StrategyWeights, analyze_view
from game.state import Personality, PlayerView, Role


class Action(StrEnum):
    """Agent 可被主持人请求的动作类型。枚举本身不授予权限，实际身份和阶段由规则再次验证。"""
    WOLF_DISCUSS = "wolf_discuss"
    WOLF_KILL = "wolf_kill"
    INSPECT = "inspect"
    SPEAK = "speak"
    VOTE = "vote"
    GUARD = "guard"
    WITCH = "witch"
    HUNTER_SHOOT = "hunter_shoot"


@dataclass(frozen=True)
class Decision:
    """Agent 的行动建议；药品类型只在女巫动作中使用。"""
    target_id: int | None = None
    text: str = ""
    suspicions: tuple[tuple[int, str], ...] = ()
    potion: str = "pass"


class PlayerAgent(ABC):
    """真人、离线策略和远程 LLM 共用的决策接口，仅接收本人快照与合法候选。"""
    human_controlled = False
    decision_source = "agent"
    role_skill: RoleSkill | None = None

    def bind_role_skill(self, skill: RoleSkill) -> None:
        """身份抽取完成后绑定本人角色知识，文档不可变且独立于可变对话记忆。"""
        self.role_skill = skill

    def observe(self, view: PlayerView) -> None:
        """主持人在私有信息更新或结算后通知玩家。"""

    @abstractmethod
    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """根据独立视图产生决策，游戏状态由主持人执行修改。"""


class MockAgent(PlayerAgent):
    """使用身份声明、查验与票型进行推理、伪装和协作的离线 Agent。"""

    decision_source = "mock"

    def __init__(self, seed: int | None = None, weights: StrategyWeights | None = None) -> None:
        """为此 Agent 创建独立随机数发生器和权重，固定 seed 时可复现其离线选择。"""
        self.rng = random.Random(seed)
        self.weights = weights or StrategyWeights()

    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """先依据本人信息选择技能分支，再进行公共讨论与阵营博弈。"""
        analysis = analyze_view(view, self.weights)
        if action is Action.GUARD:
            # 首夜空守与女巫首夜救人配合；后续在合法目标中保护高价值座位。
            if view.round_number == 1 or not candidates:
                return Decision()
            target = max(candidates, key=lambda pid: 10 * (pid in analysis.seer_claimants) - analysis.score(pid))
            return Decision(target_id=target)
        if action is Action.WITCH:
            if view.antidote_available and view.night_victim not in {None, view.player_id}:
                return Decision(target_id=view.night_victim, potion="save")
            if view.poison_available and candidates:
                target = max(candidates, key=analysis.score)
                if analysis.score(target) >= self.weights.contradiction:
                    return Decision(target_id=target, potion="poison")
            return Decision()
        if action is Action.HUNTER_SHOOT:
            if not candidates:
                return Decision()
            target = max(candidates, key=analysis.score)
            # 证据不足时保留开枪，避免死亡后的技能变成随机杀人。
            return Decision(target_id=target if analysis.score(target) > 0 else None)
        target = self._select_target(view, action, candidates, analysis)
        reason = analysis.reason(target)
        suspicions = ((target, reason),) if target is not None else ()
        text = ""
        if action is Action.WOLF_DISCUSS:
            text = (
                f"我建议今晚击杀玩家 {target}，优先处理公开跳预言家或能组织投票的对手。"
                "白天保持说法一致，视公开票势决定跟票或切割。"
            )
        elif action is Action.SPEAK:
            text = self._speech(view, target, analysis)
        return Decision(target, text, suspicions)

    def _select_target(
        self, view: PlayerView, action: Action, candidates: tuple[int, ...], analysis: StrategyAnalysis,
    ) -> int | None:
        """按动作和阵营比较候选。预言家偏好信息价值，狼人考虑伪装一致性、公开票势与队友协作。"""
        options = list(candidates)
        known = {result.target_id: result.team for result in view.inspections}
        if action is Action.INSPECT:
            options = [pid for pid in options if pid not in known] or options
            priorities = {pid: 10 * (pid in analysis.seer_claimants) + abs(analysis.score(pid)) for pid in options}
        elif view.role is Role.WEREWOLF:
            pressure = Counter(target for _, target in analysis.vote_intents)
            alive_count = sum(player.alive for player in view.players)
            doomed = [pid for pid in options if pid in view.wolf_teammates and pressure[pid] > alive_count / 2]
            can_bus = view.personality in {Personality.LOGICAL, Personality.SKEPTICAL}
            if action in {Action.SPEAK, Action.VOTE} and doomed and can_bus:
                return self.rng.choice(doomed)
            options = [pid for pid in options if pid not in view.wolf_teammates and pid != view.player_id]
            if action in {Action.WOLF_DISCUSS, Action.WOLF_KILL}:
                if action is Action.WOLF_KILL:
                    pattern = rf"第 {view.round_number} 夜，狼人队友 (\d+) 提议击杀玩家 (\d+)"
                    for note in reversed(view.private_notes):
                        match = re.search(pattern, note)
                        if match and int(match[1]) in view.wolf_teammates and int(match[2]) in options:
                            return int(match[2])
                priorities = {
                    pid: 8 * (pid in analysis.seer_claimants) + max(0, -analysis.score(pid)) * 0.2
                    for pid in options
                }
            else:
                own_reds = [
                    claim.target_id for claim in analysis.checks
                    if claim.speaker_id == view.player_id and claim.team is Team.WEREWOLF
                    and claim.target_id in options
                ]
                if own_reds:
                    return own_reds[-1]
                priorities = {pid: analysis.score(pid) + pressure[pid] for pid in options}
        else:
            priorities = {pid: analysis.score(pid) for pid in options}
        highest = max(priorities.values(), default=0)
        tied = [pid for pid in options if priorities[pid] == highest]
        return self.rng.choice(tied) if tied else None

    def _speech(self, view: PlayerView, target: int, analysis: StrategyAnalysis) -> str:
        """根据本人身份与历史公开记录生成策略性发言。真预言家可公开查验，狼人维护自己的伪装叙述。"""
        alive = {player.player_id for player in view.players if player.alive}
        own_claims = [claim for claim in analysis.checks if claim.speaker_id == view.player_id]
        if view.role is Role.SEER:
            reds = [result for result in view.inspections if result.team is Team.WEREWOLF and result.target_id in alive]
            should_reveal = reds or analysis.seer_claimants or len(alive) <= 5
            if view.inspections and should_reveal:
                result = reds[-1] if reds else view.inspections[-1]
                team = "狼人" if result.team is Team.WEREWOLF else "好人"
                return (
                    f"我是预言家，我查验玩家 {result.target_id} 为{team}。"
                    f"请对照其他人的查验声明和实际选票，我准备投玩家 {target}。"
                )
        if view.role is Role.WEREWOLF:
            fake_seer = min((view.player_id, *view.wolf_teammates)) == view.player_id
            if fake_seer:
                if own_claims:
                    last = own_claims[-1]
                    if last.round_number == view.round_number or (last.team is Team.WEREWOLF and last.target_id in alive):
                        team = "狼人" if last.team is Team.WEREWOLF else "好人"
                        return (
                            f"我坚持预言家身份，此前查验玩家 {last.target_id} 为{team}。"
                            f"请大家核对对跳者的票型，我准备投玩家 {target}。"
                        )
                    greens = [pid for pid in view.wolf_teammates if pid in alive]
                    checked = greens[0] if greens else target
                    return f"我是预言家，我查验玩家 {checked} 为好人。结合今天的发言，我准备投玩家 {target}。"
                return f"我是预言家，我查验玩家 {target} 为狼人。请玩家 {target} 回应，我准备投玩家 {target}。"
        opener = {
            Personality.AGGRESSIVE: "我直接表态",
            Personality.CAUTIOUS: "我的判断仍有不确定性",
            Personality.LOGICAL: "我先核对可验证的信息",
            Personality.LEADER: "我们需要统一检查标准",
            Personality.SKEPTICAL: "我会先检验身份声明",
        }[view.personality]
        response = "结合上一轮发言，" if view.discussion_round > 1 else ""
        debate = "场上有预言家对跳，请双方解释查验顺序。" if len(analysis.seer_claimants) > 1 else ""
        reason = analysis.reason(target)
        if view.role is Role.WEREWOLF:
            reason = "我会把公开发言和实际票型放在一起核对。"
        return f"{response}{opener}。{debate}我关注玩家 {target}，{reason}我准备投玩家 {target}。"
