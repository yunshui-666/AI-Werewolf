"""模型输出的结构与行动语义校验。"""

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from agents.player import Action, Decision
from game.rules import validate_message
from typing import Literal


class SuspicionOutput(BaseModel):
    """单个私有怀疑条目的严格输出结构，限制座位范围与理由长度。"""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    player_id: int = Field(ge=1, le=12)
    reason: str = Field(min_length=1, max_length=300)


class DecisionOutput(BaseModel):
    """模型、网页和主持人共同使用的结构化边界。形状校验之外还要结合当前行动验证目标和资源。"""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    target_id: int | None = Field(default=None, ge=1, le=12)
    text: str = Field(default="", max_length=1200)
    suspicions: list[SuspicionOutput] = Field(default_factory=list, max_length=11)
    potion: Literal["save", "poison", "pass"] = "pass"

    @model_validator(mode="after")
    def validate_action(self, info: ValidationInfo) -> "DecisionOutput":
        """利用 context 中的动作、候选和本人视图完成语义校验。药品与可选技能分别处理，重复怀疑对象被拒绝。"""
        context = info.context or {}
        action = context.get("action")
        candidates = context.get("candidates", ())
        if action in {Action.WOLF_KILL, Action.INSPECT, Action.VOTE}:
            if self.target_id not in candidates:
                raise ValueError("必须选择合法候选目标")
        if action in {Action.GUARD, Action.HUNTER_SHOOT} and self.target_id is not None:
            if self.target_id not in candidates:
                raise ValueError("技能目标必须来自合法候选列表")
        if action is Action.WITCH:
            view = context.get("view")
            if self.potion == "pass" and self.target_id is not None:
                raise ValueError("不用药时目标必须为空")
            if self.potion != "pass" and self.target_id not in candidates:
                raise ValueError("用药需要合法目标")
            if view is not None:
                if self.potion == "save" and (not view.antidote_available or self.target_id != view.night_victim or self.target_id == view.player_id):
                    raise ValueError("解药不满足使用条件")
                if self.potion == "poison" and not view.poison_available:
                    raise ValueError("毒药已经用完")
        elif self.potion != "pass":
            raise ValueError("只有女巫行动可以携带药品")
        if action in {Action.SPEAK, Action.WOLF_DISCUSS}:
            validate_message(self.text)
        allowed = context.get("suspect_ids", candidates)
        if action is not None and any(item.player_id not in allowed for item in self.suspicions):
            raise ValueError("怀疑对象必须为其他存活玩家")
        if len({item.player_id for item in self.suspicions}) != len(self.suspicions):
            raise ValueError("怀疑对象不能重复")
        return self

    def to_decision(self) -> Decision:
        """把已验证的外部结构转换成内部不可变 Decision，避免引擎依赖具体模型 SDK。"""
        return Decision(self.target_id, self.text, tuple((s.player_id, s.reason) for s in self.suspicions), self.potion)
