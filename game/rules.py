"""纯 Python 规则校验与结算计算，不调用模型，不修改游戏状态。"""

from collections import Counter
from dataclasses import dataclass
import unicodedata

from game.state import GameState, Outcome, Phase, Role


class InvalidAction(ValueError):
    """调用方提交了与当前游戏规则冲突的行动。"""


def require_phase(state: GameState, phase: Phase) -> None:
    """所有行动的阶段前置条件。已结束游戏优先拒绝，随后检查调用方需要的阶段。"""
    if state.outcome is not None or state.phase is Phase.FINISHED:
        raise InvalidAction("游戏已经结束")
    if state.phase is not phase:
        raise InvalidAction(f"当前阶段为 {state.phase.value}，需要 {phase.value}")


def require_living_player(state: GameState, player_id: int) -> None:
    """统一验证整数 ID、座位存在性与存活状态，显式拒绝 Python 中可与整数相等的 bool。"""
    if isinstance(player_id, bool) or not isinstance(player_id, int):
        raise InvalidAction("玩家 ID 必须为整数")
    if player_id not in state.players:
        raise InvalidAction(f"玩家 {player_id} 不存在")
    if not state.players[player_id].alive:
        raise InvalidAction(f"玩家 {player_id} 已死亡")


def require_other_living_target(state: GameState, actor_id: int, target_id: int) -> None:
    """先验证行动者与目标均存活，再排除自己。投票和大多数攻击技能共用这个基础规则。"""
    require_living_player(state, actor_id)
    require_living_player(state, target_id)
    if actor_id == target_id:
        raise InvalidAction("不能选择自己")


def validate_wolf_choice(state: GameState, actor_id: int, target_id: int) -> None:
    """校验本夜一次狼刀。12 人局允许策略性自刀，8 人接口保留原有目标规则。"""
    require_phase(state, Phase.NIGHT)
    require_living_player(state, actor_id)
    require_living_player(state, target_id)
    if state.preset == "classic8" and actor_id == target_id:
        raise InvalidAction("经典 8 人局不能选择自己")
    if state.players[actor_id].role is not Role.WEREWOLF:
        raise InvalidAction("只有狼人可以提交击杀选择")
    if state.preset == "classic8" and state.players[target_id].role is Role.WEREWOLF:
        raise InvalidAction("狼人不能击杀狼人队友")
    if actor_id in state.wolf_choices:
        raise InvalidAction("本夜已经提交过击杀选择")


def validate_inspection(state: GameState, actor_id: int, target_id: int) -> None:
    """检查夜间、预言家身份、目标与当夜查验次数，真实阵营查询留给引擎执行。"""
    require_phase(state, Phase.NIGHT)
    require_other_living_target(state, actor_id, target_id)
    if state.players[actor_id].role is not Role.SEER:
        raise InvalidAction("只有预言家可以查验身份")
    if state.seer_acted:
        raise InvalidAction("预言家本夜已经查验过")


def validate_speech(state: GameState, actor_id: int, text: str) -> None:
    """核对当前阶段、存活状态、文本与下一发言编号。顺序来源于本轮已经发言的记录长度。"""
    require_phase(state, Phase.DAY)
    require_living_player(state, actor_id)
    validate_message(text)
    next_speaker = state.alive_ids[len(state.speakers)]
    if actor_id != next_speaker:
        raise InvalidAction(f"当前应由玩家 {next_speaker} 发言")


def validate_message(text: str) -> None:
    """限制发言为有效非空文本，并拒绝终端控制字符。换行可输入，公开记录会进一步规整为单行。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 1200:
        raise InvalidAction("发言必须为 1 到 1200 字符的非空字符串")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text):
        raise InvalidAction("发言包含非法控制字符")


def validate_vote(state: GameState, actor_id: int, target_id: int) -> None:
    """要求投票阶段、其他存活目标以及本轮尚未投票，避免自投与重复计票。"""
    require_phase(state, Phase.VOTING)
    require_other_living_target(state, actor_id, target_id)
    if actor_id in state.votes:
        raise InvalidAction("本轮已经投票")


def choose_wolf_target(state: GameState) -> int:
    """要求所有存活狼人都提交意见。取最小存活狼人 ID 的选择，一致意见时自然得到同一结果。"""
    require_phase(state, Phase.NIGHT)
    wolves = state.alive_wolf_ids
    if not wolves or set(state.wolf_choices) != set(wolves):
        raise InvalidAction("所有存活狼人都需要提交击杀选择")
    # ID 最小的存活狼人为队长。一致意见自然也会得到同一目标。
    return state.wolf_choices[wolves[0]]


@dataclass(frozen=True)
class VoteResult:
    """纯计算的不可变计票结果，包含票数、最高票座位与唯一被放逐者。"""
    counts: tuple[tuple[int, int], ...]
    tied_ids: tuple[int, ...]
    eliminated_id: int | None


def tally_votes(state: GameState) -> VoteResult:
    """收齐合法选票后统计最高票。多个最高票时返回空放逐目标，执行层据此发布平票事件。"""
    require_phase(state, Phase.VOTING)
    if set(state.votes) != set(state.alive_ids):
        raise InvalidAction("所有存活玩家都需要完成投票")
    counts = Counter(state.votes.values())
    highest = max(counts.values())
    leaders = tuple(sorted(pid for pid, count in counts.items() if count == highest))
    eliminated_id = leaders[0] if len(leaders) == 1 else None
    return VoteResult(tuple(sorted(counts.items())), leaders, eliminated_id)


def check_outcome(state: GameState, *, round_completed: bool = False) -> Outcome | None:
    """统一胜负入口。12 人局按屠边，8 人旧版继续按人数优势。"""
    wolves = len(state.alive_wolf_ids)
    good_players = len(state.alive_ids) - wolves
    if wolves == 0:
        return Outcome.GOOD
    villagers = sum(state.players[pid].role is Role.VILLAGER for pid in state.alive_ids)
    gods = good_players - villagers
    if state.victory_rule == "sides" and (villagers == 0 or gods == 0):
        return Outcome.WEREWOLF
    if state.victory_rule == "parity" and wolves >= good_players:
        return Outcome.WEREWOLF
    if round_completed and state.round_number >= state.max_rounds:
        return Outcome.DRAW
    return None


def validate_guard(state: GameState, actor_id: int, target_id: int | None) -> None:
    """守卫可以空守、自守；连续两夜守同一座位会被拒绝。"""
    require_phase(state, Phase.NIGHT)
    require_living_player(state, actor_id)
    player = state.players[actor_id]
    if player.role is not Role.GUARD or state.guard_acted:
        raise InvalidAction("当前玩家不能再次守护")
    if target_id is not None:
        require_living_player(state, target_id)
        if target_id == player.previous_guard_target:
            raise InvalidAction("不能连续两夜守护同一玩家")


def validate_witch(state: GameState, actor_id: int, potion: str, target_id: int | None) -> None:
    """在消耗药品前完整校验。女巫每夜只能救、毒、或不用药三选一。"""
    require_phase(state, Phase.NIGHT)
    require_living_player(state, actor_id)
    player = state.players[actor_id]
    if player.role is not Role.WITCH or state.witch_action is not None:
        raise InvalidAction("当前玩家不能再次使用药品")
    if potion == "pass":
        if target_id is not None:
            raise InvalidAction("不用药时目标应为空")
    elif potion == "save":
        require_living_player(state, target_id)
        victim = choose_wolf_target(state)
        if not player.antidote_available or target_id != victim or victim == actor_id:
            raise InvalidAction("解药必须用于今晚刀口，且不能自救")
    elif potion == "poison":
        require_other_living_target(state, actor_id, target_id)
        if not player.poison_available:
            raise InvalidAction("毒药已经用完")
    else:
        raise InvalidAction("未知药品行动")


def validate_hunter(state: GameState, actor_id: int, target_id: int | None) -> None:
    """猎人是死亡后行动的唯一例外，必须持有当前死亡批次的响应资格。"""
    require_phase(state, Phase.HUNTER)
    if type(actor_id) is not int:
        raise InvalidAction("猎人 ID 必须为整数")
    if not state.pending_hunters or actor_id != state.pending_hunters[0]:
        raise InvalidAction("当前没有该猎人的开枪机会")
    player = state.players[actor_id]
    if player.role is not Role.HUNTER or player.alive or player.hunter_shot_used:
        raise InvalidAction("猎人开枪资格无效")
    if target_id is not None:
        require_living_player(state, target_id)
