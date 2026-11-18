"""每位玩家持有独立的可变记忆，传出的事件与视图保持不可变。"""

from dataclasses import dataclass, field
from enum import StrEnum


class Team(StrEnum):
    """阵营层的共享枚举，用于预言家结果和角色到阵营的映射。"""
    GOOD = "good"
    WEREWOLF = "werewolf"


@dataclass(frozen=True)
class PublicEvent:
    """不可变的公开事件。投票结果使用 tuple，多个独立记忆列表可以安全引用同一事件对象。"""
    round_number: int
    kind: str
    text: str
    actor_id: int | None = None
    target_id: int | None = None
    votes: tuple[tuple[int, int], ...] = ()
    # 只在公开发言时填写来源，夜间行动的来源与调用次数保持私有。
    decision_source: str | None = None


@dataclass(frozen=True)
class InspectionResult:
    """预言家本人拥有的不可变阵营查验记录，不保存目标精确神职。"""
    round_number: int
    target_id: int
    team: Team


@dataclass
class PlayerMemory:
    """每位玩家独立持有的记忆容器，公开历史、私有笔记、查验和怀疑判断分别管理。"""
    public_events: list[PublicEvent] = field(default_factory=list)
    private_notes: list[str] = field(default_factory=list)
    inspections: list[InspectionResult] = field(default_factory=list)
    suspicions: dict[int, str] = field(default_factory=dict)
