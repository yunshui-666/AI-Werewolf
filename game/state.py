"""主持人真实状态与可安全传给单个玩家的可见信息。"""

from dataclasses import dataclass, field
from enum import StrEnum

from agents.memory import InspectionResult, PlayerMemory, PublicEvent, Team
from agents.skills import RoleSkill


class Role(StrEnum):
    """六种身份。只有狼人属于狼人阵营，其他身份共用好人胜负。"""
    WEREWOLF = "werewolf"
    SEER = "seer"
    VILLAGER = "villager"
    WITCH = "witch"
    HUNTER = "hunter"
    GUARD = "guard"

    @property
    def team(self) -> Team:
        """将具体角色映射到阵营。预言家查验返回阵营而不是精确神职，避免技能额外泄露身份。"""
        if self is Role.WEREWOLF:
            return Team.WEREWOLF
        return Team.GOOD


class Personality(StrEnum):
    """角色之外的发言风格。同一身份可拥有不同性格，角色技能权限不会随性格改变。"""
    AGGRESSIVE = "激进型"
    CAUTIOUS = "谨慎型"
    LOGICAL = "逻辑型"
    LEADER = "领导型"
    SKEPTICAL = "怀疑型"


class Phase(StrEnum):
    """当前流程位置。HUNTER 是死亡后的受控响应阶段，用于在最终胜负前处理一次开枪。"""
    NIGHT = "night"
    DAY = "day"
    VOTING = "voting"
    FINISHED = "finished"
    HUNTER = "hunter"


class Outcome(StrEnum):
    """终局结果。空值表示游戏仍在进行，平局由最大完整轮数触发。"""
    GOOD = "good"
    WEREWOLF = "werewolf"
    DRAW = "draw"


@dataclass
class PlayerState:
    """主持人保存的单人真实状态，药品和猎人技能均为整局资源。"""
    player_id: int
    role: Role
    personality: Personality
    alive: bool = True
    memory: PlayerMemory = field(default_factory=PlayerMemory)
    role_skill: RoleSkill | None = None
    antidote_available: bool = True
    poison_available: bool = True
    hunter_shot_used: bool = False
    previous_guard_target: int | None = None


@dataclass
class GameState:
    """仅供主持人和规则引擎使用，禁止直接传给模型。"""

    players: dict[int, PlayerState]
    max_rounds: int
    round_number: int = 1
    phase: Phase = Phase.NIGHT
    outcome: Outcome | None = None
    public_events: list[PublicEvent] = field(default_factory=list)
    wolf_choices: dict[int, int] = field(default_factory=dict)
    wolf_discussions: dict[int, str] = field(default_factory=dict)
    seer_acted: bool = False
    speakers: list[int] = field(default_factory=list)
    votes: dict[int, int] = field(default_factory=dict)
    discussion_round: int = 1
    discussion_rounds: int = 1
    preset: str = "classic8"
    victory_rule: str = "parity"
    guard_acted: bool = False
    guard_target: int | None = None
    witch_action: str | None = None
    witch_target: int | None = None
    # 一次死亡批次中的猎人全部响应后，才执行胜负判定与阶段推进。
    pending_hunters: list[int] = field(default_factory=list)
    resolution_kind: str | None = None
    death_causes: dict[int, str] = field(default_factory=dict)

    @property
    def alive_ids(self) -> tuple[int, ...]:
        """按编号返回当前存活座位，作为发言顺序和大部分候选目标的统一依据。"""
        return tuple(sorted(pid for pid, player in self.players.items() if player.alive))

    @property
    def alive_wolf_ids(self) -> tuple[int, ...]:
        """主持人内部用于狼人行动与胜负判定的序列，不直接进入好人玩家视图。"""
        return tuple(
            pid for pid in self.alive_ids if self.players[pid].role is Role.WEREWOLF
        )


@dataclass(frozen=True)
class VisiblePlayer:
    """其他座位的最小公开表示，只携带编号和存活状态，不携带真实角色或记忆引用。"""
    player_id: int
    alive: bool


@dataclass(frozen=True)
class PlayerView:
    """单个玩家的白名单快照。技能资源字段只反映该玩家本人。"""
    player_id: int
    role: Role
    personality: Personality
    alive: bool
    round_number: int
    phase: Phase
    players: tuple[VisiblePlayer, ...]
    wolf_teammates: tuple[int, ...]
    public_events: tuple[PublicEvent, ...]
    private_notes: tuple[str, ...]
    inspections: tuple[InspectionResult, ...]
    suspicions: tuple[tuple[int, str], ...]
    discussion_round: int = 1
    discussion_rounds: int = 1
    preset: str = "classic8"
    player_count: int = 8
    role_skill: RoleSkill | None = None
    antidote_available: bool = False
    poison_available: bool = False
    night_victim: int | None = None
    previous_guard_target: int | None = None
    hunter_can_shoot: bool = False


def build_player_view(state: GameState, player_id: int) -> PlayerView:
    """显式白名单构造快照，禁止从真实状态整体序列化后再删除字段。"""
    player = state.players[player_id]
    teammates = ()
    if player.role is Role.WEREWOLF:
        teammates = tuple(
            pid
            for pid, other in sorted(state.players.items())
            if pid != player_id and other.role is Role.WEREWOLF
        )
    return PlayerView(
        player_id=player_id,
        role=player.role,
        personality=player.personality,
        alive=player.alive,
        round_number=state.round_number,
        phase=state.phase,
        players=tuple(
            VisiblePlayer(pid, other.alive)
            for pid, other in sorted(state.players.items())
        ),
        wolf_teammates=teammates,
        public_events=tuple(player.memory.public_events),
        private_notes=tuple(player.memory.private_notes),
        inspections=tuple(player.memory.inspections),
        suspicions=tuple(sorted(player.memory.suspicions.items())),
        discussion_round=state.discussion_round,
        discussion_rounds=state.discussion_rounds,
        preset=state.preset,
        player_count=len(state.players),
        role_skill=player.role_skill,
        antidote_available=player.role is Role.WITCH and player.antidote_available,
        poison_available=player.role is Role.WITCH and player.poison_available,
        night_victim=(
            state.wolf_choices.get(state.alive_wolf_ids[0])
            if player.role is Role.WITCH and player.antidote_available
            and state.phase is Phase.NIGHT and state.alive_wolf_ids
            and set(state.wolf_choices) == set(state.alive_wolf_ids)
            else None
        ),
        previous_guard_target=player.previous_guard_target if player.role is Role.GUARD else None,
        hunter_can_shoot=player_id in state.pending_hunters,
    )
