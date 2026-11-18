"""行动执行边界。规则通过后才写入真实状态与对应记忆。"""

import logging
import random
from dataclasses import asdict
import json
from threading import RLock

from agents.memory import InspectionResult, PublicEvent
from config import GameConfig
from config import PRESETS
from agents.skills import load_role_skill
from game.rules import (
    InvalidAction,
    VoteResult,
    check_outcome,
    choose_wolf_target,
    require_living_player,
    require_phase,
    validate_message,
    tally_votes,
    validate_inspection,
    validate_speech,
    validate_vote,
    validate_wolf_choice,
    validate_guard,
    validate_witch,
    validate_hunter,
)
from game.state import (
    GameState,
    Outcome,
    Personality,
    Phase,
    PlayerState,
    PlayerView,
    Role,
    build_player_view,
)

logger = logging.getLogger("werewolf_arena.public")
admin_logger = logging.getLogger("werewolf_arena.admin")

ROLE_NAMES = {
    Role.WEREWOLF: "狼人", Role.SEER: "预言家", Role.VILLAGER: "村民",
    Role.WITCH: "女巫", Role.HUNTER: "猎人", Role.GUARD: "守卫",
}
OUTCOME_NAMES = {Outcome.GOOD: "好人获胜", Outcome.WEREWOLF: "狼人获胜", Outcome.DRAW: "平局"}


class GameEngine:
    """主持人引擎是唯一执行真实规则的入口。Agent 只给建议，所有资源消耗、死亡和阶段推进都在这里完成。"""
    def __init__(self, config: GameConfig | None = None) -> None:
        """创建真实状态并为每个座位装载与底牌一致的策略 skill。"""
        self.config = config or GameConfig()
        # 网页读取与主持人写入共用此锁；等待真人输入时不会持有它。
        self.lock = RLock()
        # 网页会话可以附加公开日志接收器，回调只接收已公开的事件对象。
        self.public_sink = None
        rng = random.Random(self.config.seed)
        roles = [Role(name) for name, count in PRESETS[self.config.preset].items() for _ in range(count)]
        rng.shuffle(roles)
        players = {
            pid: PlayerState(pid, role, rng.choice(tuple(Personality)))
            for pid, role in enumerate(roles, start=1)
        }
        self.state = GameState(
            players=players,
            max_rounds=self.config.max_rounds,
            discussion_rounds=self.config.discussion_rounds,
            preset=self.config.preset,
            victory_rule="sides" if self.config.preset == "standard12" else "parity",
        )
        wolf_ids = self.state.alive_wolf_ids
        for player in players.values():
            player.role_skill = load_role_skill(player.role.value)
            player.antidote_available = player.role is Role.WITCH
            player.poison_available = player.role is Role.WITCH
            player.memory.private_notes.append(f"你的身份是{ROLE_NAMES[player.role]}。")
            if player.role is Role.WEREWOLF:
                teammates = [pid for pid in wolf_ids if pid != player.player_id]
                player.memory.private_notes.append(f"你的狼人队友 ID 为 {teammates}。")
        self._publish("game_started", f"游戏开始，共 {self.config.player_count} 名玩家。")
        self._publish("night_started", "========== 第 1 夜 ==========")
        self._debug_state("初始真实状态")

    def player_view(self, player_id: int) -> PlayerView:
        """为指定玩家生成独立白名单视图。调用方只能把这个快照交给该玩家，不能传入完整 GameState。"""
        return build_player_view(self.state, player_id)

    def wolf_discuss(self, actor_id: int, text: str) -> None:
        """记录一名存活狼人的一次私聊，并仅同步给存活狼人队友。重复发言和非狼人调用会先失败。"""
        require_phase(self.state, Phase.NIGHT)
        require_living_player(self.state, actor_id)
        if self.state.players[actor_id].role is not Role.WEREWOLF:
            raise InvalidAction("只有狼人可以参与私下交流")
        if actor_id in self.state.wolf_discussions:
            raise InvalidAction("本夜已经参与过私下交流")
        validate_message(text)
        normalized = " ".join(text.split())
        self.state.wolf_discussions[actor_id] = normalized
        note = f"第 {self.state.round_number} 夜，狼人 {actor_id} 私下发言：{normalized}"
        for pid in self.state.alive_wolf_ids:
            self.state.players[pid].memory.private_notes.append(note)
        admin_logger.debug("%s", note)

    def remember_suspicions(self, actor_id: int, suspicions: tuple[tuple[int, str], ...]) -> None:
        """校验所有怀疑对象后统一写回本人记忆。待响应的死亡猎人可以保留推断，其他死亡玩家不能行动。"""
        require_phase(self.state, self.state.phase)
        if actor_id not in self.state.pending_hunters:
            require_living_player(self.state, actor_id)
        for target_id, reason in suspicions:
            require_living_player(self.state, target_id)
            if target_id == actor_id or not isinstance(reason, str) or not reason.strip():
                raise InvalidAction("怀疑记录需要指向其他存活玩家并提供理由")
        self.state.players[actor_id].memory.suspicions.update(suspicions)

    def submit_wolf_choice(self, actor_id: int, target_id: int) -> None:
        """保存本夜击杀意见，队友从私有笔记看到该选择。最终目标在结算时按固定队长裁决。"""
        validate_wolf_choice(self.state, actor_id, target_id)
        self.state.wolf_choices[actor_id] = target_id
        admin_logger.debug("第 %s 夜，狼人 %s 提议击杀玩家 %s。", self.state.round_number, actor_id, target_id)
        # 狼人协作信息只写入狼人私有记忆。
        for pid in self.state.alive_wolf_ids:
            self.state.players[pid].memory.private_notes.append(
                f"第 {self.state.round_number} 夜，狼人队友 {actor_id} 提议击杀玩家 {target_id}。"
            )

    def inspect(self, actor_id: int, target_id: int) -> InspectionResult:
        """真实阵营只写入预言家本人记忆，不进入公开事件。"""
        validate_inspection(self.state, actor_id, target_id)
        result = InspectionResult(
            self.state.round_number, target_id, self.state.players[target_id].role.team
        )
        self.state.players[actor_id].memory.inspections.append(result)
        self.state.seer_acted = True
        admin_logger.debug("第 %s 夜，预言家 %s 查验玩家 %s，阵营为 %s。", result.round_number, actor_id, target_id, result.team.value)
        return result

    def guard(self, actor_id: int, target_id: int | None) -> None:
        """记录本夜守护并更新该守卫自己的上一夜目标。"""
        validate_guard(self.state, actor_id, target_id)
        self.state.guard_target = target_id
        self.state.guard_acted = True
        self.state.players[actor_id].previous_guard_target = target_id
        target_text = f"玩家 {target_id}" if target_id is not None else "空守"
        self.state.players[actor_id].memory.private_notes.append(
            f"第 {self.state.round_number} 夜守护选择为 {target_text}。"
        )

    def witch(self, actor_id: int, potion: str, target_id: int | None) -> None:
        """药品在提交有效行动后消耗，重复调用会先被规则拒绝。"""
        validate_witch(self.state, actor_id, potion, target_id)
        player = self.state.players[actor_id]
        if potion == "save":
            player.antidote_available = False
        elif potion == "poison":
            player.poison_available = False
        self.state.witch_action = potion
        self.state.witch_target = target_id
        player.memory.private_notes.append(
            f"第 {self.state.round_number} 夜药品行动为 {potion}，目标 {target_id}。"
        )

    def resolve_night(self) -> int | tuple[int, ...]:
        """合并狼刀、守护和药品后一次性结算死亡，猎人响应期间延后胜负。"""
        target_id = choose_wolf_target(self.state)
        seer_alive = any(
            player.alive and player.role is Role.SEER
            for player in self.state.players.values()
        )
        if seer_alive and not self.state.seer_acted:
            raise InvalidAction("存活预言家需要先完成本夜查验")
        for role, completed in ((Role.GUARD, self.state.guard_acted), (Role.WITCH, self.state.witch_action is not None)):
            if not completed and any(p.alive and p.role is role for p in self.state.players.values()):
                raise InvalidAction("所有存活神职都需要先提交本夜行动")
        saved = self.state.witch_action == "save" and self.state.witch_target == target_id
        guarded = self.state.guard_target == target_id
        deaths = {}
        # 单独守护或解药都能抵挡狼刀，同时作用时按本版房规仍然死亡。
        if not (saved ^ guarded):
            deaths[target_id] = "wolf"
        if self.state.witch_action == "poison":
            deaths[self.state.witch_target] = "poison"
        for pid, cause in sorted(deaths.items()):
            self._kill(pid, cause)
            self._publish("night_death", f"主持人：昨夜玩家 {pid} 死亡。", target_id=pid)
        if not deaths:
            self._publish("peaceful_night", "主持人：昨夜平安夜。")
        self._after_deaths("night")
        # 保持旧 8 人调用方的返回类型；新板子允许平安夜和多名夜间死者。
        return target_id if self.state.preset == "classic8" else tuple(sorted(deaths))

    def speak(self, actor_id: int, text: str, *, decision_source: str | None = None) -> None:
        """要求当前编号的存活玩家发言，公开规范化文本。最后一人结束后开始下一轮讨论或进入投票。"""
        validate_speech(self.state, actor_id, text)
        self.state.speakers.append(actor_id)
        self._publish("speech", f"玩家 {actor_id}：{' '.join(text.split())}", actor_id=actor_id, decision_source=decision_source)
        if len(self.state.speakers) == len(self.state.alive_ids):
            if self.state.discussion_round < self.state.discussion_rounds:
                self.state.discussion_round += 1
                self.state.speakers.clear()
                self._start_discussion_round()
            else:
                self.state.phase = Phase.VOTING
                self._publish("voting_started", "========== 投票 ==========")

    def _start_discussion_round(self) -> None:
        """将本轮发言顺序作为公开事件发布，序列只包含当前存活座位。"""
        order = " → ".join(f"玩家 {pid}" for pid in self.state.alive_ids)
        self._publish(
            "discussion_round_started",
            f"第 {self.state.discussion_round}/{self.state.discussion_rounds} 轮发言，顺序为 {order}。",
        )

    def cast_vote(self, actor_id: int, target_id: int) -> None:
        """保存一张经过合法性校验的选票。收齐前暂存在主持人状态中，其他玩家看不到即时票型。"""
        validate_vote(self.state, actor_id, target_id)
        self.state.votes[actor_id] = target_id

    def resolve_votes(self) -> VoteResult:
        """收齐选票后统一公开并结算最高票。平票无人出局，放逐触发的猎人技能会暂缓阶段推进。"""
        result = tally_votes(self.state)
        # 收齐选票后统一公开，后投票的 Agent 看不到尚未结算的选票。
        votes = tuple(sorted(self.state.votes.items()))
        text = "\n".join(f"玩家 {actor} -> 玩家 {target}" for actor, target in votes)
        self._publish("vote_result", text, votes=votes)
        if result.eliminated_id is None:
            self._publish("vote_tie", "最高票并列，本轮无人被放逐。")
        else:
            self._kill(result.eliminated_id, "exile")
            self._publish(
                "exile", f"玩家 {result.eliminated_id} 被放逐。", target_id=result.eliminated_id
            )
        self._after_deaths("vote")
        return result

    def hunter_shoot(self, actor_id: int, target_id: int | None) -> None:
        """按队列处理死亡猎人的可选开枪，再恢复当前批次结算。"""
        validate_hunter(self.state, actor_id, target_id)
        self.state.pending_hunters.pop(0)
        self.state.players[actor_id].hunter_shot_used = True
        if target_id is not None:
            self._kill(target_id, "hunter")
            self._publish(
                "hunter_shot", f"玩家 {actor_id} 发动猎人技能，带走玩家 {target_id}。",
                actor_id=actor_id, target_id=target_id,
            )
        if not self.state.pending_hunters:
            self._advance_resolution(self.state.resolution_kind)

    def _kill(self, player_id: int, cause: str) -> None:
        """一次死亡只入队一次；中毒猎人直接失去开枪资格。"""
        player = self.state.players[player_id]
        player.alive = False
        self.state.death_causes[player_id] = cause
        if player.role is Role.HUNTER and cause != "poison" and not player.hunter_shot_used:
            self.state.pending_hunters.append(player_id)

    def _after_deaths(self, kind: str) -> None:
        """暂存结算来源，避免猎人技能处理完后丢失应返回的阶段。"""
        self.state.resolution_kind = kind
        if self.state.pending_hunters:
            self.state.phase = Phase.HUNTER
        else:
            self._advance_resolution(kind)

    def _advance_resolution(self, kind: str) -> None:
        """所有死亡响应结束后，先判胜负，再进入白天或下一晚。"""
        self.state.resolution_kind = None
        if self._finish_if_needed(round_completed=kind == "vote"):
            return
        if kind == "night":
            self.state.phase = Phase.DAY
            self._publish("day_started", f"========== 第 {self.state.round_number} 天 ==========")
            self._start_discussion_round()
        else:
            self.state.round_number += 1
            self.state.phase = Phase.NIGHT
            self.state.wolf_choices.clear()
            self.state.wolf_discussions.clear()
            self.state.seer_acted = False
            self.state.speakers.clear()
            self.state.discussion_round = 1
            self.state.votes.clear()
            self.state.guard_acted = False
            self.state.guard_target = None
            self.state.witch_action = None
            self.state.witch_target = None
            self._publish("night_started", f"========== 第 {self.state.round_number} 夜 ==========")

    def final_roles(self) -> dict[int, Role]:
        """终局开放全部身份给复盘界面。游戏进行中调用会被拒绝，防止展示层提前亮牌。"""
        if self.state.phase is not Phase.FINISHED:
            raise InvalidAction("游戏结束后才能公开全部身份")
        return {pid: player.role for pid, player in sorted(self.state.players.items())}

    def _finish_if_needed(self, *, round_completed: bool = False) -> bool:
        """按本局胜利条件与轮数上限决定是否结束。终局后写入公开结果，并为管理员记录最终真实状态。"""
        outcome = check_outcome(self.state, round_completed=round_completed)
        if outcome is None:
            return False
        self.state.outcome = outcome
        self.state.phase = Phase.FINISHED
        self._publish("game_finished", f"游戏结束：{OUTCOME_NAMES[outcome]}。")
        self._debug_state("最终真实状态")
        return True

    def _debug_state(self, label: str) -> None:
        """只有管理员 logger 启用时才序列化完整状态。普通公开事件与玩家上下文不调用这条输出路径。"""
        if admin_logger.isEnabledFor(logging.DEBUG):
            admin_logger.debug("%s\n%s", label, json.dumps(asdict(self.state), ensure_ascii=False, indent=2))

    def _publish(
        self,
        kind: str,
        text: str,
        *,
        actor_id: int | None = None,
        target_id: int | None = None,
        votes: tuple[tuple[int, int], ...] = (),
        decision_source: str | None = None,
    ) -> None:
        """创建不可变公开事件，同步到各玩家各自的历史列表，并输出公开日志。事件对象可共享，列表容器各自独立。"""
        event = PublicEvent(self.state.round_number, kind, text, actor_id, target_id, votes, decision_source)
        self.state.public_events.append(event)
        for player in self.state.players.values():
            player.memory.public_events.append(event)
        logger.info("%s", text)
        if decision_source is not None:
            logger.info("公开发言来源为 %s。", decision_source)
        if self.public_sink is not None:
            self.public_sink(event)
