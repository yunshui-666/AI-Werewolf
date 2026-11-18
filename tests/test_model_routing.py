"""多模型默认路由、真实来源标记与对话上下文的离线回归测试。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.memory import PublicEvent
from agents.player import Action
from agents.prompts import build_messages
from config import GameConfig
from game.engine import GameEngine
from game.runner import GameRunner
from llm.profiles import APIProfile, ProfileRegistry
from tests.test_runner import BrokenAgent
from web.session import GameSession, SessionOptions


class RoutingTests(unittest.TestCase):
    """每个测试只使用临时 .env 和假客户端，避免读取真实凭据或访问网络。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def env(self, text):
        (self.root / ".env").write_text(text, encoding="utf-8")

    def configured(self):
        self.env(
            "DEEPSEEK_API_KEY=SECRET_MARKER\n"
            "QWEN_API_KEY=OTHER_SECRET\nQWEN_BASE_URL=https://example.com/v1\nQWEN_MODEL=qwen-test\n"
            "AGENT_3_PROVIDER=qwen\nLLM_TEMPERATURE=0.8\nQWEN_TEMPERATURE=0.6\nLLM_TIMEOUT=7\n"
        )
        return ProfileRegistry(self.root)

    def test_discovers_groups_and_seat_override_without_returning_keys(self):
        registry = self.configured()
        self.assertEqual(registry.resolve_selections(3, {}), {1: "deepseek", 2: "deepseek", 3: "qwen"})
        payload = json.dumps([registry.public_profiles(), registry.routing_setup()])
        self.assertNotIn("SECRET_MARKER", payload)
        self.assertNotIn("OTHER_SECRET", payload)
        self.assertEqual({p["profile_id"] for p in registry.public_profiles()}, {"mock", "deepseek", "qwen"})

    def test_explicit_selection_overrides_seat_env(self):
        registry = self.configured()
        self.assertEqual(registry.resolve_selections(3, {2: "mock", 3: "deepseek"}), {1: "deepseek", 2: "mock", 3: "deepseek"})

    def test_global_default_and_process_environment_take_precedence(self):
        registry = self.configured()
        with patch.dict(os.environ, {"LLM_DEFAULT_PROVIDER": "qwen", "AGENT_3_PROVIDER": "mock"}):
            self.assertEqual(registry.resolve_selections(3, {}), {1: "qwen", 2: "qwen", 3: "mock"})

    def test_unconfigured_default_is_offline_and_key_alone_is_not_a_generic_profile(self):
        self.env("UNRELATED_API_KEY=SECRET_MARKER\n")
        registry = ProfileRegistry(self.root)
        self.assertEqual(registry.resolve_selections(2, {}), {1: "mock", 2: "mock"})
        self.assertNotIn("unrelated", registry.profiles)

    def test_missing_key_or_unknown_explicit_provider_has_safe_startup_error(self):
        for text in ("LLM_DEFAULT_PROVIDER=deepseek\n", "LLM_DEFAULT_PROVIDER=absent\n"):
            with self.subTest(text=text):
                self.env(text)
                registry = ProfileRegistry(self.root)
                with self.assertRaises(ValueError):
                    registry.create_agents(2, {}, None)
                self.assertTrue(registry.routing_setup(2)["config_warnings"])

    def test_invalid_env_url_does_not_fall_back_to_default_endpoint(self):
        self.env("DEEPSEEK_API_KEY=SECRET_MARKER\nDEEPSEEK_BASE_URL=https://user:SECRET_MARKER@example.com\n")
        registry = ProfileRegistry(self.root)
        self.assertNotIn("deepseek", registry.profiles)
        self.assertNotIn("SECRET_MARKER", json.dumps(registry.routing_setup()))
        with self.assertRaises(ValueError):
            registry.resolve_selections(1, {1: "deepseek"})

    def test_discovers_new_env_and_removes_deleted_profile_without_restart(self):
        registry = ProfileRegistry(self.root)
        self.configured()
        self.assertEqual(registry.resolve_selections(3, {})[3], "qwen")
        self.env("")
        self.assertEqual(registry.resolve_selections(3, {}), {1: "mock", 2: "mock", 3: "mock"})
        self.assertNotIn("qwen", registry.profiles)

    def test_auto_agents_share_connection_and_honor_global_and_provider_parameters(self):
        registry = self.configured()
        with patch("llm.profiles.DeepSeekClient") as factory:
            factory.side_effect = lambda config, pacer=None: type("Client", (), {"config": config, "pacer": pacer, "close": lambda self: None})()
            agents, clients = registry.create_agents(4, {}, 42)
        self.assertEqual(len(clients), 2)
        self.assertIs(clients[0].pacer, clients[1].pacer)
        self.assertEqual(clients[0].pacer.interval, 1.0)
        self.assertIs(agents[1].client, agents[2].client)
        self.assertIsNot(agents[1].client, agents[3].client)
        self.assertEqual(agents[3].profile_id, "qwen")
        self.assertEqual(agents[1].client.config.temperature, 0.8)
        self.assertEqual(agents[3].client.config.temperature, 0.6)
        self.assertEqual(agents[3].client.config.timeout, 7)

    def test_case_sensitive_saved_ids_and_local_keyless_profile_remain_supported(self):
        registry = ProfileRegistry(self.root)
        registry.save(APIProfile(profile_id="LocalModel", name="local", base_url="http://localhost:8000/v1", model="local", requires_key=False))
        self.assertEqual(registry.resolve_selections(1, {1: "LocalModel"}), {1: "LocalModel"})
        with patch("llm.profiles.DeepSeekClient") as factory:
            agents, _ = registry.create_agents(1, {}, None)
        self.assertEqual(agents[1].profile_id, "LocalModel")
        self.assertEqual(factory.call_args.args[0].api_key, "local-no-key")

    def test_saved_profile_metadata_keeps_precedence_over_same_env_prefix(self):
        registry = self.configured()
        profile = APIProfile(profile_id="deepseek", name="saved", base_url="https://saved.example.com/v1", model="saved-model", key_env="SAVED_KEY")
        registry.save(profile, "RUNTIME_SECRET")
        actual = next(item for item in registry.public_profiles() if item["profile_id"] == "deepseek")
        self.assertEqual(actual["model"], "saved-model")
        self.assertEqual(actual["base_url"], "https://saved.example.com/v1")
        self.assertTrue(actual["has_key"])
        self.assertNotIn("RUNTIME_SECRET", json.dumps(actual))

    def test_invalid_numeric_parameters_do_not_expose_values(self):
        registry = self.configured()
        with patch.dict(os.environ, {"QWEN_TIMEOUT": "SECRET_MARKER"}):
            with self.assertRaises(ValueError) as captured:
                registry.create_agents(3, {1: "mock", 2: "mock"}, None)
        self.assertNotIn("SECRET_MARKER", str(captured.exception))

    def test_session_reports_resolved_model_and_skips_human_provider(self):
        registry = self.configured()
        with patch.dict(os.environ, {"AGENT_1_PROVIDER": "absent"}), patch("llm.profiles.DeepSeekClient"):
            session = GameSession(SessionOptions(preset="classic8", seed=42), registry)
        snapshot = session.snapshot()
        self.assertEqual(snapshot["players"][0]["profile_id"], "human")
        self.assertEqual(snapshot["players"][1]["profile_id"], "deepseek")
        self.assertEqual(snapshot["players"][2]["profile_id"], "qwen")
        self.assertIsNone(snapshot["fallback_count"])
        self.assertIsNone(snapshot["model_requests"])
        self.assertTrue(all(p["role"] is None for p in snapshot["players"] if not p["human"]))
        for client in session.clients:
            client.close()


class DeepSeekPoolTests(unittest.TestCase):
    """多 Key 分配只使用假凭据，检查循环顺序、真人跳过与固定座位优先级。"""

    setUp = RoutingTests.setUp
    env = RoutingTests.env

    def pool(self, extra=""):
        self.env(
            "DEEPSEEK_API_KEY=key-one\nDEEPSEEK_API2=key-two\nDEEPSEEK_API3=key-three\n"
            "DEEPSEEK_BASE_URL=https://example.com/v1\nDEEPSEEK_MODEL=shared-model\n"
            "DEEPSEEK_TEMPERATURE=0.6\nDEEPSEEK_THINKING=disabled\n" + extra
        )
        return ProfileRegistry(self.root)

    def test_aliases_inherit_metadata_and_secrets_stay_private(self):
        registry = self.pool()
        profiles = {profile["profile_id"]: profile for profile in registry.public_profiles()}
        self.assertEqual(profiles["deepseek_2"]["key_env"], "DEEPSEEK_API2")
        self.assertEqual(profiles["deepseek_3"]["base_url"], "https://example.com/v1")
        self.assertEqual(profiles["deepseek_3"]["model"], "shared-model")
        public = json.dumps([profiles, registry.routing_setup()])
        for key in ("key-one", "key-two", "key-three"):
            self.assertNotIn(key, public)

    def test_round_robin_skips_human_and_reuses_profile_connection(self):
        registry = self.pool()
        with patch("llm.profiles.DeepSeekClient") as factory:
            def create(config, pacer=None):
                return type("Client", (), {"config": config, "pacer": pacer, "close": lambda self: None})()
            factory.side_effect = create
            agents, clients = registry.create_agents(8, {1: "mock"}, 42)
        self.assertEqual([agents[pid].profile_id for pid in range(1, 9)], [
            "mock", "deepseek", "deepseek_2", "deepseek_3", "deepseek", "deepseek_2", "deepseek_3", "deepseek",
        ])
        self.assertEqual(len(clients), 3)
        self.assertIs(agents[2].client, agents[5].client)
        self.assertIsNot(agents[2], agents[5])
        self.assertIsNot(agents[2].client, agents[3].client)
        self.assertEqual(agents[3].client.config.api_key, "key-two")
        self.assertEqual(agents[3].client.config.temperature, 0.6)
        self.assertEqual(agents[3].client.config.thinking, "disabled")
        self.assertTrue(all(client.pacer is clients[0].pacer for client in clients))
        self.assertEqual(clients[0].pacer.interval, 1.0)

    def test_numeric_order_empty_entries_and_primary_absent(self):
        self.env("DEEPSEEK_API10=ten\nDEEPSEEK_API2=two\nDEEPSEEK_API3=   \nLLM_DEFAULT_PROVIDER=deepseek\n")
        registry = ProfileRegistry(self.root)
        self.assertEqual(registry.resolve_selections(4, {}), {
            1: "deepseek_2", 2: "deepseek_10", 3: "deepseek_2", 4: "deepseek_10",
        })
        self.assertEqual(registry.routing_setup()["deepseek_pool"], ["deepseek_2", "deepseek_10"])

    def test_explicit_and_env_seats_do_not_consume_automatic_pool(self):
        registry = self.pool("AGENT_3_PROVIDER=deepseek_3\n")
        self.assertEqual(registry.resolve_selections(6, {1: "mock", 4: "deepseek_2"}), {
            1: "mock", 2: "deepseek", 3: "deepseek_3", 4: "deepseek_2", 5: "deepseek_2", 6: "deepseek_3",
        })

    def test_preview_matches_actual_route_for_different_human_seats(self):
        registry = self.pool()
        for human in (0, 1, 3, 8):
            with self.subTest(human=human):
                selections = {2: "deepseek_3", 5: "mock"}
                preview = registry.routing_setup(8, human, selections)
                if human:
                    selections[human] = "mock"
                expected = registry.resolve_selections(8, selections)
                self.assertEqual(preview["seat_defaults"], {str(pid): profile for pid, profile in expected.items()})

    def test_new_keys_are_detected_for_next_game_without_restart(self):
        registry = self.pool()
        self.env("DEEPSEEK_API_KEY=main\nDEEPSEEK_API7=seven\n")
        self.assertEqual(registry.resolve_selections(3, {1: "mock"}), {1: "mock", 2: "deepseek", 3: "deepseek_7"})
        self.assertNotIn("deepseek_2", registry.profiles)

    def test_other_default_provider_keeps_its_explicit_priority(self):
        registry = self.pool("QWEN_API_KEY=qwen\nQWEN_BASE_URL=https://qwen.example.com\nQWEN_MODEL=qwen\nLLM_DEFAULT_PROVIDER=qwen\n")
        self.assertEqual(registry.resolve_selections(2, {}), {1: "qwen", 2: "qwen"})

    def test_deepseek_cli_mode_uses_pool(self):
        import main
        from types import SimpleNamespace
        registry = self.pool()
        with patch("llm.profiles.ProfileRegistry", return_value=registry), patch("llm.profiles.DeepSeekClient") as factory:
            factory.side_effect = lambda config, pacer=None: SimpleNamespace(
                config=config, pacer=pacer, request_count=0, total_tokens=0, close=lambda: None,
            )
            with patch("sys.argv", ["main.py", "--mode", "deepseek", "--auto", "--preset", "classic8"]), patch("main.configure_logging", return_value=SimpleNamespace(public="test.log", admin=None)), patch("main.final_summary", return_value=""), patch("workflow.graph.run_graph") as run:
                self.assertEqual(main.main(), 0)
            runner = run.call_args.args[0]
        self.assertEqual([runner.agents[pid].profile_id for pid in range(1, 5)], ["deepseek", "deepseek_2", "deepseek_3", "deepseek"])


class ConversationTests(unittest.TestCase):
    def test_speech_focus_contains_previous_conversation_and_private_info_stays_local(self):
        engine = GameEngine(GameConfig(seed=42))
        view = engine.player_view(1)
        events = (
            PublicEvent(1, "speech", "玩家 1：我暂时相信 3 号。", 1),
            PublicEvent(1, "speech", "玩家 2：玩家 12 怎么还不表态？", 2),
            PublicEvent(1, "speech", "玩家 3：1号，为什么相信我？", 3),
            PublicEvent(1, "speech", "玩家 4：玩家 1，你能解释一下吗？", 4),
        )
        own_view = replace(view, public_events=events)
        messages = build_messages(own_view, Action.SPEAK, (2, 3, 4))
        payload = json.loads(messages[1]["content"])
        self.assertNotIn("strategy_analysis", payload)
        focus = payload["dialogue_context"]
        self.assertEqual(len(focus["recent_speeches"]), 4)
        self.assertEqual(len(focus["own_previous_speeches"]), 1)
        self.assertEqual([e["actor_id"] for e in focus["recent_mentions_of_you"]], [3, 4])
        # 在其他玩家私有记忆中写入标记，重建本人的 Prompt 后依旧不可见。
        engine.state.players[2].memory.private_notes.append("OTHER_PRIVATE_MARKER")
        rebuilt = build_messages(engine.player_view(1), Action.SPEAK, (2, 3, 4))
        self.assertNotIn("OTHER_PRIVATE_MARKER", json.dumps(rebuilt))
        self.assertIn("strategy_analysis", json.loads(build_messages(own_view, Action.VOTE, (2, 3, 4))[1]["content"]))

    def test_fallback_is_labelled_only_on_public_speech(self):
        runner = GameRunner(GameConfig(seed=42, max_rounds=1), {pid: BrokenAgent() for pid in range(1, 9)})
        with self.assertLogs("werewolf_arena.public", level="WARNING"):
            state = runner.run()
        speeches = [event for event in state.public_events if event.kind == "speech"]
        self.assertTrue(speeches)
        self.assertTrue(all(event.decision_source == "fallback" for event in speeches))
        self.assertTrue(all(event.decision_source is None for event in state.public_events if event.kind != "speech"))

    def test_explicit_mock_speech_has_truthful_source(self):
        state = GameRunner(GameConfig(seed=42, max_rounds=1)).run()
        self.assertTrue(all(event.decision_source == "mock" for event in state.public_events if event.kind == "speech"))
