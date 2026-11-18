"""人类玩家的终端输入，使用与 AI 相同的个人可见视图。"""

from collections.abc import Callable
import re

from agents.memory import Team
from agents.player import Action, Decision, PlayerAgent
from game.rules import InvalidAction, validate_message
from game.state import PlayerView, Role

ROLE_LABELS = {
    Role.WEREWOLF: "狼人", Role.SEER: "预言家", Role.VILLAGER: "村民",
    Role.WITCH: "女巫", Role.HUNTER: "猎人", Role.GUARD: "守卫",
}
ACTION_LABELS = {
    Action.WOLF_DISCUSS: "狼人私聊",
    Action.WOLF_KILL: "选择击杀目标",
    Action.INSPECT: "选择查验目标",
    Action.SPEAK: "公开发言",
    Action.VOTE: "选择放逐目标",
    Action.GUARD: "守卫守护",
    Action.WITCH: "女巫用药",
    Action.HUNTER_SHOOT: "猎人开枪",
}


class HumanAgent(PlayerAgent):
    """终端真人适配器。输入与私有提示只面向操作者，游戏规则依然由主持人统一校验。"""
    human_controlled = True

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        """接收可注入的输入输出函数，初始化私有消息的已读游标，便于在测试中替换真实终端。"""
        self.input_fn = input_fn
        self.output = output_fn
        self.initialized = False
        self.notes_seen = 0
        self.inspections_seen = 0
        self.was_alive = True

    def observe(self, view: PlayerView) -> None:
        """根据已读游标展示新增的本人私聊或查验结果，避免每次输入重复刷屏，并在出局后切换观战提示。"""
        if not self.initialized:
            self.output(f"\n【仅你可见】你是玩家 {view.player_id}，身份为{ROLE_LABELS[view.role]}。")
            if view.wolf_teammates:
                teammates = "、".join(str(pid) for pid in view.wolf_teammates)
                self.output(f"【仅你可见】狼人队友为玩家 {teammates}。")
            self.output("轮到你时输入发言或目标编号。/help 查看命令，/quit 退出。")
            self.initialized = True
            self.notes_seen = len(view.private_notes)
        for note in view.private_notes[self.notes_seen:]:
            self.output(f"【仅你可见】{note}")
        self.notes_seen = len(view.private_notes)
        for result in view.inspections[self.inspections_seen:]:
            team = "狼人" if result.team is Team.WEREWOLF else "好人"
            self.output(f"【仅你可见】第 {result.round_number} 夜，查验玩家 {result.target_id} 为{team}。")
        self.inspections_seen = len(view.inspections)
        if self.was_alive and not view.alive:
            self.output("\n你已出局，接下来继续观看本局的公开过程。")
        self.was_alive = view.alive

    def decide(self, view: PlayerView, action: Action, candidates: tuple[int, ...]) -> Decision:
        """根据本人视图、当前动作和合法候选生成一次决策。输出只携带行动建议，真实状态由主持人更新。"""
        if not view.alive and not view.hunter_can_shoot:
            raise InvalidAction("死亡玩家不能行动")
        self.observe(view)
        self.output(f"\n轮到玩家 {view.player_id}（你）进行{ACTION_LABELS[action]}。")
        if action is Action.SPEAK:
            self.output(f"今天第 {view.discussion_round}/{view.discussion_rounds} 轮发言。")
        if action not in {Action.SPEAK, Action.WOLF_DISCUSS}:
            self.output("合法目标为 " + "、".join(str(pid) for pid in candidates))
        if action is Action.WITCH:
            self.output(f"【仅你可见】解药 {int(view.antidote_available)} 瓶，毒药 {int(view.poison_available)} 瓶，刀口 {view.night_victim}。")
            self.output("输入 save 编号 救人、poison 编号 毒人，或 0 不用药。")
        elif action in {Action.GUARD, Action.HUNTER_SHOOT}:
            self.output("输入目标编号，或 0 放弃本次技能。")
        while True:
            value = self.input_fn("你的输入 > ").strip()
            if value.startswith("/"):
                if value == "/quit":
                    raise KeyboardInterrupt
                if value == "/help":
                    self.output("/status 查看本人状态，/history 查看公开历史，/pass 暂时保留发言，/quit 退出。")
                elif value == "/status":
                    self._show_status(view)
                elif value == "/history":
                    for event in view.public_events:
                        self.output(event.text)
                elif value == "/pass" and action in {Action.SPEAK, Action.WOLF_DISCUSS}:
                    return Decision(text="本轮暂时保留判断，我会继续观察。")
                elif value == "/pass" and action in {Action.WITCH, Action.GUARD, Action.HUNTER_SHOOT}:
                    return Decision()
                else:
                    self.output("当前命令不可用，输入 /help 查看帮助。")
                continue
            if value == "0" and action in {Action.WITCH, Action.GUARD, Action.HUNTER_SHOOT}:
                return Decision()
            if action in {Action.SPEAK, Action.WOLF_DISCUSS}:
                try:
                    validate_message(value)
                except InvalidAction:
                    self.output("请输入 1 到 1200 字符的有效发言。")
                    continue
                return Decision(text=value)
            if action is Action.WITCH:
                match = re.fullmatch(r"(save|poison)\s+(\d+)", value)
                if match:
                    potion, target = match[1], int(match[2])
                    save_ok = potion == "save" and view.antidote_available and target == view.night_victim and target != view.player_id
                    poison_ok = potion == "poison" and view.poison_available and target in candidates
                    if save_ok or poison_ok:
                        return Decision(target_id=target, potion=potion)
                self.output("用药无效，请核对药品、刀口和目标，或输入 0。")
                continue
            match = re.fullmatch(r"(?:玩家\s*)?(\d+)(?:\s*号)?", value)
            target = int(match[1]) if match else None
            if target not in candidates:
                self.output("目标无效，请输入列表中其他存活玩家的编号。")
                continue
            return Decision(target_id=target)

    def _show_status(self, view: PlayerView) -> None:
        """按需显示本人角色、记忆与查验记录。命令只读取当前玩家已有快照，不访问其他角色状态。"""
        alive = "、".join(str(player.player_id) for player in view.players if player.alive)
        self.output(f"【仅你可见】玩家 {view.player_id}，身份为{ROLE_LABELS[view.role]}。存活玩家为 {alive}。")
        for note in view.private_notes:
            self.output(f"【仅你可见】{note}")
        for result in view.inspections:
            team = "狼人" if result.team is Team.WEREWOLF else "好人"
            self.output(f"【仅你可见】第 {result.round_number} 夜查验玩家 {result.target_id} 为{team}。")
