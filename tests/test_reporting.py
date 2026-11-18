"""终局展示、日志信息边界与 CLI 正常入口回归。

用子进程从真实命令行入口检查公开日志、管理员文件和终局展示。固定 classic8 作为兼容场景，新角色另由扩展测试覆盖。
"""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from config import GameConfig
from game.reporting import final_summary
from game.engine import OUTCOME_NAMES
from game.rules import InvalidAction
from game.runner import GameRunner
from tests.test_rules import EngineTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def test_summary_requires_finished_game(self) -> None:
        runner = GameRunner(GameConfig(seed=42))
        with self.assertRaises(InvalidAction):
            final_summary(runner.engine)

    def test_final_summary_contains_roles_and_timeline(self) -> None:
        runner = GameRunner(GameConfig(seed=42))
        runner.run()
        text = final_summary(runner.engine)
        self.assertIn(OUTCOME_NAMES[runner.engine.state.outcome], text)
        self.assertIn("过程回顾", text)
        self.assertIn("玩家 5：预言家", text)
        self.assertIn("主持人：昨夜玩家", text)
        self.assertNotIn("私下发言", text)

    def run_cli(self, log_dir: Path, *extra):
        environment = {**os.environ, "PYTHONUTF8": "1"}
        return subprocess.run(
            [sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "--mode", "mock", "--preset", "classic8", "--auto", "--discussion-rounds", "1", "--seed", "42", "--log-dir", str(log_dir), *extra],
            cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def test_default_cli_finishes_and_writes_public_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.run_cli(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("终局总结", result.stdout)
            self.assertIn("langgraph", result.stdout)
            self.assertEqual(len(list(path.glob("game_*.log"))), 1)
            self.assertEqual(list(path.glob("admin_*.log")), [])
            public = next(path.glob("game_*.log")).read_text(encoding="utf-8")
            self.assertIn("全部身份", public)
            self.assertLess(public.index("游戏结束："), public.index("全部身份"))
            for marker in ("私下发言", "提议击杀", "初始真实状态", "决策为"):
                self.assertNotIn(marker, public)

    def test_admin_debug_log_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            result = self.run_cli(path, "--debug")
            self.assertEqual(result.returncode, 0, result.stderr)
            admin = next(path.glob("admin_*.log")).read_text(encoding="utf-8")
            public = next(path.glob("game_*.log")).read_text(encoding="utf-8")
            for marker in ("私下发言", "提议击杀", "初始真实状态", "最终真实状态", "决策为"):
                self.assertIn(marker, admin)
                self.assertNotIn(marker, public)
                self.assertNotIn(marker, result.stdout)

    def test_python_workflow_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory), "--workflow", "python", "--max-rounds", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("平局", result.stdout)

    def test_invalid_cli_configuration_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory), "--max-rounds", "0")
            self.assertEqual(result.returncode, 2)
            self.assertIn("启动配置无效", result.stderr)
            self.assertEqual(list(Path(directory).glob("*.log")), [])


class MessageSafetyTests(EngineTestCase):
    def test_overlong_and_terminal_control_messages_are_rejected(self) -> None:
        for text in ("x" * 1201, "\x1b[2J", "\x00"):
            with self.subTest(text=text[:10]):
                self.assert_rejected_without_change(self.engine.wolf_discuss, self.wolves[0], text)
        self.reach_day()
        actor = self.state.alive_ids[0]
        self.assert_rejected_without_change(self.engine.speak, actor, "\x1b[2J")

    def test_public_speech_is_normalized_to_one_line(self) -> None:
        self.reach_day()
        actor = self.state.alive_ids[0]
        self.engine.speak(actor, "我的判断\n\t仍需要验证。")
        self.assertEqual(self.state.public_events[-1].text, f"玩家 {actor}：我的判断 仍需要验证。")
