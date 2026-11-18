"""真人输入、私有输出、顺序发言与退出行为。

为真人 Agent 注入可控输入输出，检查重输、命令、私有结果和退出。CLI 子进程验证真实 stdin 的交互顺序。
"""

from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from agents.human import HumanAgent
from agents.player import Action
from config import GameConfig
from game.runner import GameRunner
from game.rules import InvalidAction
from game.state import Phase, Role
from workflow.graph import run_graph

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HumanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = GameRunner(GameConfig(seed=42))
        self.engine = self.runner.engine
        self.view = self.engine.player_view(1)

    def human(self, inputs):
        output = []
        values = iter(inputs)
        return HumanAgent(lambda prompt: next(values), output.append), output

    def test_vote_reprompts_until_other_living_target_is_selected(self) -> None:
        human, output = self.human(["1", "99", "狼人", "玩家 3"])
        result = human.decide(self.view, Action.VOTE, (2, 3, 4))
        self.assertEqual(result.target_id, 3)
        self.assertEqual(sum("目标无效" in text for text in output), 3)

    def test_empty_and_control_speech_are_reprompted(self) -> None:
        human, _ = self.human([" ", "\x1b[2J", "我希望玩家 3 解释一下。"])
        result = human.decide(self.view, Action.SPEAK, (2, 3))
        self.assertEqual(result.text, "我希望玩家 3 解释一下。")

    def test_status_history_help_and_pass(self) -> None:
        human, output = self.human(["/status", "/history", "/help", "/pass"])
        result = human.decide(self.view, Action.SPEAK, (2, 3))
        self.assertIn("保留判断", result.text)
        self.assertTrue(any("游戏开始" in text for text in output))
        self.assertTrue(any("/status" in text for text in output))
        self.assertFalse(any("狼人队友为" in text for text in output))

    def test_quit_and_eof_preserve_human_control(self) -> None:
        human, _ = self.human(["/quit"])
        with self.assertRaises(KeyboardInterrupt):
            human.decide(self.view, Action.SPEAK, (2, 3))

        def closed(prompt):
            raise EOFError

        self.runner.agents[1] = HumanAgent(closed, lambda text: None)
        with self.assertRaises(EOFError):
            self.runner._decide(1, Action.SPEAK)
        self.assertEqual(self.runner.fallback_count, 0)

    def test_dead_human_cannot_act(self) -> None:
        human, _ = self.human([])
        with self.assertRaises(InvalidAction):
            human.decide(replace(self.view, alive=False), Action.SPEAK, (2, 3))

    def test_inspection_reaches_only_human_console(self) -> None:
        seer = next(pid for pid, player in self.engine.state.players.items() if player.role is Role.SEER)
        wolf = self.engine.state.alive_wolf_ids[0]
        human, output = self.human([])
        human.observe(self.engine.player_view(seer))
        self.engine.inspect(seer, wolf)
        human.observe(self.engine.player_view(seer))
        human.observe(self.engine.player_view(seer))
        checks = [text for text in output if "查验玩家" in text]
        self.assertEqual(len(checks), 1)
        self.assertIn(f"查验玩家 {wolf} 为狼人", checks[0])
        self.assertFalse(any("查验" in event.text for event in self.engine.state.public_events))

    def test_human_can_play_all_three_roles_to_completion(self) -> None:
        for role in (Role.WEREWOLF, Role.SEER, Role.VILLAGER):
            with self.subTest(role=role):
                runner = GameRunner(GameConfig(seed=42, discussion_rounds=2))
                pid = next(pid for pid, player in runner.engine.state.players.items() if player.role is role)
                output = []

                def read(prompt):
                    if output[-1].startswith("合法目标为 "):
                        return output[-1].removeprefix("合法目标为 ").split("、")[0]
                    return "我会继续观察玩家的发言和票型。"

                runner.agents[pid] = HumanAgent(read, output.append)
                runner.notify_humans()
                with self.assertLogs("werewolf_arena.public", level="INFO") as captured:
                    state = run_graph(runner)
                self.assertIs(state.phase, Phase.FINISHED)
                self.assertEqual(runner.fallback_count, 0)
                self.assertNotIn("【仅你可见】", "\n".join(captured.output))

    def test_default_cli_is_human_and_can_quit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "--mode", "mock", "--preset", "classic8", "--seed", "42", "--log-dir", directory],
                input="/quit\n", capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONUTF8": "1"}, cwd=PROJECT_ROOT, timeout=60,
            )
            self.assertEqual(result.returncode, 130, result.stderr)
            self.assertIn("你控制玩家 1", result.stdout)
            self.assertIn("轮到玩家 1（你）", result.stdout)
            public = next(Path(directory).glob("game_*.log")).read_text(encoding="utf-8")
            self.assertNotIn("【仅你可见】", public)

    def test_interactive_cli_can_finish_with_scripted_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "--mode", "mock", "--preset", "classic8", "--seed", "42", "--max-rounds", "1", "--log-dir", directory],
                input="\n".join(["我准备投玩家 7。", "我准备投玩家 7。", "7"] * 20) + "\n",
                capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"},
                cwd=PROJECT_ROOT, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("轮到玩家 1（你）进行选择放逐目标", result.stdout)
            self.assertIn("终局总结", result.stdout)
            self.assertIn("降级行动 0 次", result.stdout)


class DiscussionTests(unittest.TestCase):
    def test_every_discussion_round_follows_living_player_order(self) -> None:
        runner = GameRunner(GameConfig(seed=42, discussion_rounds=2))
        runner.night_actions()
        runner.resolve_night()
        alive = runner.engine.state.alive_ids
        runner.day_discussion()
        speeches = [event.actor_id for event in runner.engine.state.public_events if event.kind == "speech"]
        self.assertEqual(speeches, list(alive) * 2)
        self.assertIs(runner.engine.state.phase, Phase.VOTING)
        self.assertEqual(runner.engine.player_view(1).discussion_round, 2)

    def test_day_cannot_advance_before_all_discussion_rounds_complete(self) -> None:
        runner = GameRunner(GameConfig(seed=42, discussion_rounds=2))
        runner.night_actions()
        runner.resolve_night()
        for pid in runner.engine.state.alive_ids:
            runner.engine.speak(pid, "第一轮发言")
        self.assertIs(runner.engine.state.phase, Phase.DAY)
        self.assertEqual(runner.engine.state.discussion_round, 2)
        self.assertEqual(runner.engine.state.speakers, [])

    def test_invalid_discussion_rounds(self) -> None:
        for value in (0, -1, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GameConfig(discussion_rounds=value)
