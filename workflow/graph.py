"""LangGraph 管理流程，主持人引擎保存真实状态并确定性执行规则。"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from game.runner import GameRunner
from game.state import GameState, Phase


class WorkflowState(TypedDict):
    """图编排只保存轮次、阶段与结果元数据，完整身份和私有记忆留在主持人引擎中。"""
    round_number: int
    phase: str
    outcome: str | None


def build_graph(runner: GameRunner):
    """把已有主持人方法连接成有条件分支的 LangGraph，所有节点共用相同的确定性规则。"""
    def snapshot() -> WorkflowState:
        """生成当前流程元数据快照，图的返回值不包含角色分配或私有技能结果。"""
        state = runner.engine.state
        return {
            "round_number": state.round_number,
            "phase": state.phase.value,
            "outcome": state.outcome.value if state.outcome else None,
        }

    def initialize(_state: WorkflowState) -> WorkflowState:
        """把已创建的主持人状态同步到图入口，实际随机分配在 GameEngine 初始化时完成。"""
        return snapshot()

    def night_actions(_state: WorkflowState) -> WorkflowState:
        """调用主持人按依赖执行守卫、狼人、女巫和预言家行动，再同步流程位置。"""
        runner.night_actions()
        return snapshot()

    def night_resolution(_state: WorkflowState) -> WorkflowState:
        """合并夜间效果并处理猎人响应后，向路由提供当前最终阶段。"""
        runner.resolve_night()
        return snapshot()

    def day_discussion(_state: WorkflowState) -> WorkflowState:
        """按存活 ID 顺序执行配置数量的讨论轮次，只有引擎完成最后一轮后才退出 DAY。"""
        runner.day_discussion()
        return snapshot()

    def voting(_state: WorkflowState) -> WorkflowState:
        """对每名尚未投票的存活玩家请求一次决定，选票由引擎暂存，决策时不会把其他待结算选票传出去。"""
        runner.voting()
        return snapshot()

    def vote_resolution(_state: WorkflowState) -> WorkflowState:
        """执行投票结算和可能的猎人响应，结果由下一节点进行流程路由。"""
        runner.resolve_votes()
        return snapshot()

    def victory_check(_state: WorkflowState) -> WorkflowState:
        # 每次结算的引擎操作已判定胜负，此节点依据结果进行下一步路由。
        """读取引擎已经完成的胜负判断，保持规则只在引擎中维护一份。"""
        return snapshot()

    def after_night(state: WorkflowState) -> str:
        """夜晚已经终局时直接进入结束路由，否则开始白天讨论。"""
        return "victory_check" if state["phase"] == Phase.FINISHED.value else "day_discussion"

    def after_check(state: WorkflowState) -> str:
        """终局连接 END，未结束连接下一夜，使完整游戏自动循环。"""
        return END if state["phase"] == Phase.FINISHED.value else "night_actions"

    graph = StateGraph(WorkflowState)
    graph.add_node("initialize", initialize)
    graph.add_node("night_actions", night_actions)
    graph.add_node("night_resolution", night_resolution)
    graph.add_node("day_discussion", day_discussion)
    graph.add_node("voting", voting)
    graph.add_node("vote_resolution", vote_resolution)
    graph.add_node("victory_check", victory_check)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "night_actions")
    graph.add_edge("night_actions", "night_resolution")
    graph.add_conditional_edges("night_resolution", after_night, {
        "victory_check": "victory_check", "day_discussion": "day_discussion",
    })
    graph.add_edge("day_discussion", "voting")
    graph.add_edge("voting", "vote_resolution")
    graph.add_edge("vote_resolution", "victory_check")
    graph.add_conditional_edges("victory_check", after_check, {END: END, "night_actions": "night_actions"})
    return graph.compile()


def run_graph(runner: GameRunner) -> GameState:
    """执行已编译流程并按最大轮数给出足够的图步数预算，已结束游戏直接返回原状态。"""
    if runner.engine.state.phase is Phase.FINISHED:
        return runner.engine.state
    graph = build_graph(runner)
    graph.invoke({}, config={"recursion_limit": runner.engine.state.max_rounds * 7 + 4})
    return runner.engine.state
