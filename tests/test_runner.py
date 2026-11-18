"""离线多局、Agent 信息边界和异常降级。

主持人测试覆盖多局自动运行、错误 Agent 的合法降级以及只传入个人视图的调用契约。异常原文包含测试标记，便于验证日志脱敏。
"""

import unittest

from agents.player import Action, Decision, PlayerAgent
from config import GameConfig
from game.runner import GameRunner
from game.state import Phase, Role
from tests.test_rules import EngineTestCase


class BrokenAgent(PlayerAgent):
    def decide(self, view, action, candidates):
        raise RuntimeError("PRIVATE_ERROR_SECRET")


class IllegalAgent(PlayerAgent):
    def decide(self, view, action, candidates):
        return Decision(target_id=view.player_id, text="", suspicions=())


class RunnerTests(unittest.TestCase):
    def test_one_hundred_games_finish_with_legal_actions(self) -> None:
        for seed in range(100):
            with self.subTest(seed=seed):
                runner = GameRunner(GameConfig(seed=seed, max_rounds=5))
                state = runner.run()
                self.assertIs(state.phase, Phase.FINISHED)
                self.assertIsNotNone(state.outcome)
                self.assertLessEqual(state.round_number, 5)
                self.assertEqual(runner.fallback_count, 0)

    def test_seed_reproduces_entire_game(self) -> None:
        a = GameRunner(GameConfig(seed=42))
        b = GameRunner(GameConfig(seed=42))
        self.assertEqual(a.run(), b.run())

    def test_failed_agent_falls_back_without_leaking_exception(self) -> None:
        runner = GameRunner(GameConfig(seed=42), {pid: BrokenAgent() for pid in range(1, 9)})
        with self.assertLogs("werewolf_arena.public", level="WARNING") as captured:
            state = runner.run()
        self.assertIs(state.phase, Phase.FINISHED)
        self.assertGreater(runner.fallback_count, 0)
        self.assertNotIn("PRIVATE_ERROR_SECRET", "\n".join(captured.output))

    def test_illegal_output_falls_back(self) -> None:
        runner = GameRunner(GameConfig(seed=42), {pid: IllegalAgent() for pid in range(1, 9)})
        with self.assertLogs("werewolf_arena.public", level="WARNING"):
            self.assertIs(runner.run().phase, Phase.FINISHED)
        self.assertGreater(runner.fallback_count, 0)

    def test_night_fallback_logs_do_not_reveal_who_has_a_skill(self) -> None:
        runner = GameRunner(GameConfig(seed=42), {pid: BrokenAgent() for pid in range(1, 9)})
        with self.assertLogs("werewolf_arena.public", level="WARNING") as captured:
            runner.night_actions()
        public = "\n".join(captured.output)
        for hidden in ("玩家", "wolf_discuss", "wolf_kill", "inspect", "PRIVATE_ERROR_SECRET"):
            self.assertNotIn(hidden, public)

    def test_runner_delivers_only_player_view(self) -> None:
        calls = []

        class RecordingAgent(IllegalAgent):
            def decide(self, view, action, candidates):
                calls.append((view, action, candidates))
                return super().decide(view, action, candidates)

        runner = GameRunner(GameConfig(seed=42), {pid: RecordingAgent() for pid in range(1, 9)})
        with self.assertLogs("werewolf_arena.public", level="WARNING"):
            runner.run()
        for view, action, candidates in calls:
            self.assertTrue(view.alive)
            self.assertFalse(hasattr(view, "wolf_choices"))
            for player in view.players:
                self.assertFalse(hasattr(player, "role"))
            self.assertNotIn(view.player_id, candidates)
            if action in {Action.WOLF_DISCUSS, Action.WOLF_KILL}:
                self.assertIs(view.role, Role.WEREWOLF)
                self.assertFalse(set(candidates) & set(view.wolf_teammates))


class WolfDiscussionTests(EngineTestCase):
    def test_discussion_is_private_and_once_per_wolf(self) -> None:
        wolf = self.wolves[0]
        self.engine.wolf_discuss(wolf, "PRIVATE_WOLF_DISCUSSION")
        self.assert_rejected_without_change(self.engine.wolf_discuss, wolf, "第二次交流")
        self.assert_rejected_without_change(self.engine.wolf_discuss, self.seer, "窃听")
        for pid, player in self.state.players.items():
            notes = "\n".join(self.engine.player_view(pid).private_notes)
            self.assertEqual("PRIVATE_WOLF_DISCUSSION" in notes, player.role is Role.WEREWOLF)
        self.assertNotIn("PRIVATE_WOLF_DISCUSSION", "\n".join(e.text for e in self.state.public_events))

    def test_invalid_suspicions_are_atomic(self) -> None:
        self.assert_rejected_without_change(
            self.engine.remember_suspicions, self.seer,
            ((self.wolves[0], "合法理由"), (999, "非法目标")),
        )
