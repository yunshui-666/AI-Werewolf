"""确认图编排复用规则并在夜晚终局时正确跳过白天。

同种子下比较 LangGraph 与顺序执行的真实结果，保证图只编排流程。单独构造夜间终局验证不会继续白天。
"""

import unittest

from config import GameConfig
from game.runner import GameRunner
from game.state import Outcome, Phase, Role
from workflow.graph import build_graph, run_graph


class WorkflowTests(unittest.TestCase):
    def test_graph_matches_python_runner_across_seeds(self) -> None:
        for seed in range(50):
            with self.subTest(seed=seed):
                python_runner = GameRunner(GameConfig(seed=seed))
                graph_runner = GameRunner(GameConfig(seed=seed))
                self.assertEqual(run_graph(graph_runner), python_runner.run())
                self.assertEqual(graph_runner.fallback_count, 0)

    def test_night_victory_routes_directly_to_end(self) -> None:
        runner = GameRunner(GameConfig(seed=42))
        villagers = [player for player in runner.engine.state.players.values() if player.role is Role.VILLAGER]
        for player in villagers[:3]:
            player.alive = False
        state = run_graph(runner)
        self.assertIs(state.outcome, Outcome.WEREWOLF)
        self.assertEqual(state.public_events[-2].kind, "night_death")
        self.assertEqual(state.public_events[-1].kind, "game_finished")

    def test_round_cap_terminates_graph(self) -> None:
        state = run_graph(GameRunner(GameConfig(seed=42, max_rounds=1)))
        self.assertIs(state.phase, Phase.FINISHED)
        self.assertIs(state.outcome, Outcome.DRAW)
        self.assertEqual(state.round_number, 1)

    def test_graph_outputs_only_flow_metadata(self) -> None:
        runner = GameRunner(GameConfig(seed=42))
        graph = build_graph(runner)
        output = graph.invoke({}, {"recursion_limit": 100})
        self.assertEqual(set(output), {"round_number", "phase", "outcome"})
        self.assertEqual(output["phase"], "finished")
        self.assertEqual(output["outcome"], runner.engine.state.outcome.value)
        self.assertIn("voting", graph.get_graph().nodes)

    def test_finished_game_is_returned_without_actions(self) -> None:
        runner = GameRunner(GameConfig(seed=42))
        state = run_graph(runner)
        count = len(state.public_events)
        self.assertIs(run_graph(runner), state)
        self.assertEqual(len(state.public_events), count)
