"""以真实 SDK 和本地 HTTP 模拟传输验证模型调用，测试全程离线。

使用 OpenAI SDK 的本地 HTTP 模拟传输验证接口格式、重试和结构化输出。所有 Key 均为测试字符串，不会调用真实模型服务。
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

from agents.player import Action
from agents.prompts import build_messages
from agents.schemas import DecisionOutput
from config import GameConfig, LLMConfig
from game.runner import GameRunner
from game.state import Phase, Role
from llm.client import DeepSeekClient, LLMPlayerAgent, LLMUnavailable, RequestPacer


def completion(content, finish_reason="stop"):
    return {
        "id": "offline-test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = GameRunner(GameConfig(seed=42))
        self.pid = next(pid for pid, p in self.runner.engine.state.players.items() if p.role is Role.VILLAGER)
        self.view = self.runner.engine.player_view(self.pid)
        self.candidates = tuple(p.player_id for p in self.view.players if p.player_id != self.pid)

    def client(self, handler, retries=0):
        sdk = OpenAI(
            api_key="offline-test-key", base_url="https://api.deepseek.com", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(sdk.close)
        return DeepSeekClient(LLMConfig(api_key="offline-test-key", max_retries=retries), sdk, pacer=RequestPacer(0))

    def test_success_uses_configured_endpoint_and_json_format(self) -> None:
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion(json.dumps({"target_id": self.candidates[0]})))

        client = self.client(handler)
        decision = client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(decision.target_id, self.candidates[0])
        self.assertEqual(requests[0].url.host, "api.deepseek.com")
        body = json.loads(requests[0].content)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(client.total_tokens, 15)

    def test_deepseek_default_disables_thinking_and_custom_endpoint_keeps_default(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion(json.dumps({"target_id": self.candidates[0]})))

        client = self.client(handler)
        client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
        other = DeepSeekClient(replace(client.config, base_url="https://other.example.com/v1"), client.sdk)
        self.assertEqual(other.request_options, {})
        explicit = DeepSeekClient(replace(client.config, thinking="enabled"), client.sdk)
        self.assertEqual(explicit.request_options, {"extra_body": {"thinking": {"type": "enabled"}}})

    def test_invalid_outputs_are_rejected(self) -> None:
        contents = (
            "", "   ", None, "broken-json", "[]", "```json\n{}\n```",
            json.dumps({"target_id": self.pid}),
            json.dumps({"target_id": 999}),
            json.dumps({"target_id": True}),
            json.dumps({"target_id": str(self.candidates[0])}),
            json.dumps({"target_id": self.candidates[0], "unexpected": "secret"}),
        )
        for content in contents:
            with self.subTest(content=content):
                client = self.client(lambda request: httpx.Response(200, json=completion(content)))
                with self.assertRaises(LLMUnavailable):
                    client.decide(self.view, Action.VOTE, self.candidates)

    def test_truncated_response_is_rejected(self) -> None:
        client = self.client(lambda request: httpx.Response(200, json=completion('{"target_id":1}', "length")))
        with self.assertRaises(LLMUnavailable):
            client.decide(self.view, Action.VOTE, self.candidates)

    def test_rate_limit_retries_then_succeeds(self) -> None:
        responses = [
            httpx.Response(429, json={"error": {"message": "PRIVATE_SERVER_MESSAGE"}}),
            httpx.Response(200, json=completion(json.dumps({"target_id": self.candidates[0]}))),
        ]
        client = self.client(lambda request: responses.pop(0), retries=1)
        with patch("llm.client.time.sleep"), self.assertLogs("werewolf_arena.public", level="WARNING") as captured:
            result = client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(result.target_id, self.candidates[0])
        self.assertEqual(client.request_count, 2)
        self.assertNotIn("PRIVATE_SERVER_MESSAGE", "\n".join(captured.output))
        self.assertNotIn(f"玩家 {self.pid}", "\n".join(captured.output))

    def test_bad_json_retries_then_succeeds(self) -> None:
        contents = ["invalid", json.dumps({"target_id": self.candidates[0]})]
        client = self.client(lambda request: httpx.Response(200, json=completion(contents.pop(0))), retries=1)
        with patch("llm.client.time.sleep"), self.assertLogs("werewolf_arena.public", level="WARNING"):
            self.assertEqual(client.decide(self.view, Action.VOTE, self.candidates).target_id, self.candidates[0])

    def test_schema_retry_supplies_safe_correction_without_echoing_bad_response(self):
        requests = []
        contents = [
            json.dumps({"type": "SECRET_MODEL_TEXT", "target_id": self.candidates[0]}),
            json.dumps({"target_id": self.candidates[0]}),
        ]

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion(contents.pop(0)))

        client = self.client(handler, retries=1)
        with patch("llm.client.time.sleep"), self.assertLogs("werewolf_arena.public", level="WARNING"):
            decision = client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(decision.target_id, self.candidates[0])
        self.assertEqual(len(requests[1]["messages"]), 3)
        self.assertIn("四个字段", requests[1]["messages"][-1]["content"])
        self.assertNotIn("SECRET_MODEL_TEXT", json.dumps(requests[1]))

    def test_authentication_error_disables_further_requests(self) -> None:
        client = self.client(lambda request: httpx.Response(401, json={"error": {"message": "SECRET"}}), retries=2)
        for _ in range(2):
            with self.assertRaises(LLMUnavailable):
                client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(client.request_count, 1)
        self.assertTrue(client.disabled)

    def test_http_failure_log_explains_category_without_server_text(self):
        client = self.client(lambda request: httpx.Response(403, json={"error": {"message": "PRIVATE_SERVER_MESSAGE"}}))
        with self.assertLogs("werewolf_arena.public", level="WARNING") as captured:
            with self.assertRaises(LLMUnavailable):
                client.decide(self.view, Action.VOTE, self.candidates)
        message = "\n".join(captured.output)
        self.assertIn("HTTP 403", message)
        self.assertIn("权限", message)
        self.assertNotIn("PRIVATE_SERVER_MESSAGE", message)

    def test_timeout_has_bounded_retries(self) -> None:
        def timeout(request):
            raise httpx.ReadTimeout("PRIVATE_REQUEST", request=request)

        client = self.client(timeout, retries=1)
        with patch("llm.client.time.sleep"), self.assertLogs("werewolf_arena.public", level="WARNING"):
            with self.assertRaises(LLMUnavailable):
                client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(client.request_count, 2)

    def test_repeated_failures_disable_requests_for_rest_of_game(self) -> None:
        client = self.client(lambda request: httpx.Response(500, json={"error": {"message": "outage"}}))
        for _ in range(4):
            with self.assertRaises(LLMUnavailable):
                client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(client.request_count, 3)
        self.assertTrue(client.disabled)

    def test_success_resets_consecutive_failure_count(self) -> None:
        responses = [
            httpx.Response(500, json={"error": {"message": "temporary"}}),
            httpx.Response(200, json=completion(json.dumps({"target_id": self.candidates[0]}))),
        ]
        client = self.client(lambda request: responses.pop(0))
        with self.assertRaises(LLMUnavailable):
            client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(client.failed_decisions, 1)
        client.decide(self.view, Action.VOTE, self.candidates)
        self.assertEqual(client.failed_decisions, 0)

    def test_schema_rejects_terminal_control_in_public_speech(self) -> None:
        with self.assertRaises(ValidationError):
            DecisionOutput.model_validate({"text": "\x1b[2J"}, context={"action": Action.SPEAK})

    def test_full_llm_agent_game_with_simulated_http(self) -> None:
        def handler(request):
            body = json.loads(request.content)
            data = json.loads(body["messages"][1]["content"])
            target = data["legal_target_ids"][0]
            result = {"target_id": target, "text": "我会结合公开信息继续判断。", "suspicions": []}
            return httpx.Response(200, json=completion(json.dumps(result, ensure_ascii=False)))

        client = self.client(handler)
        runner = GameRunner(GameConfig(seed=42), {pid: LLMPlayerAgent(client) for pid in range(1, 9)})
        self.assertIs(runner.run().phase, Phase.FINISHED)
        self.assertEqual(runner.fallback_count, 0)
        self.assertGreater(client.request_count, 10)
        self.assertTrue(all(
            event.decision_source == "api"
            for event in runner.engine.state.public_events if event.kind == "speech"
        ))

    def test_prompt_contains_only_current_players_private_data(self) -> None:
        state = self.runner.engine.state
        seer = next(pid for pid, p in state.players.items() if p.role is Role.SEER)
        state.players[seer].memory.private_notes.append("SEER_SECRET_MARKER")
        messages = build_messages(self.runner.engine.player_view(self.pid), Action.SPEAK, self.candidates)
        self.assertNotIn("SEER_SECRET_MARKER", json.dumps(messages))
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["visible_information"]["player_id"], self.pid)
        self.assertEqual(set(payload["visible_information"]["players"][0]), {"player_id", "alive"})
        self.assertNotIn("votes", payload["visible_information"])

    def test_suspicions_are_validated_as_private_structured_output(self) -> None:
        context = {"action": Action.VOTE, "candidates": self.candidates}
        invalid = (
            {"player_id": self.pid, "reason": "自己"},
            {"player_id": self.candidates[0], "reason": " "},
            {"player_id": True, "reason": "非法类型"},
        )
        for suspicion in invalid:
            with self.subTest(suspicion=suspicion), self.assertRaises(ValidationError):
                DecisionOutput.model_validate({"target_id": self.candidates[0], "suspicions": [suspicion]}, context=context)


class RequestPacerTests(unittest.TestCase):
    """用虚拟时钟验证 1 秒间隔，测试自身无需真实睡眠。"""

    def test_waits_one_second_after_previous_response_and_skips_elapsed_wait(self):
        clock = [10.0]
        waits = []
        def sleep(delay):
            waits.append(delay)
            clock[0] += delay
        pacer = RequestPacer()
        with patch("llm.client.time.monotonic", side_effect=lambda: clock[0]), patch("llm.client.time.sleep", side_effect=sleep):
            with pacer.slot():
                clock[0] += 3
            with pacer.slot():
                self.assertEqual(clock[0], 14)
                clock[0] += 2
            clock[0] += 4
            with pacer.slot():
                self.assertEqual(clock[0], 20)
        self.assertEqual(waits, [1.0])

    def test_failed_request_keeps_interval_and_releases_lock(self):
        clock = [10.0]
        def sleep(delay):
            clock[0] += delay
        pacer = RequestPacer()
        with patch("llm.client.time.monotonic", side_effect=lambda: clock[0]), patch("llm.client.time.sleep", side_effect=sleep) as sleeper:
            with self.assertRaises(RuntimeError), pacer.slot():
                raise RuntimeError("test")
            with pacer.slot():
                self.assertEqual(clock[0], 11)
        sleeper.assert_called_once_with(1.0)


class ConfigTests(unittest.TestCase):
    def test_api_key_is_hidden_from_repr(self) -> None:
        self.assertNotIn("MY_API_SECRET", repr(LLMConfig(api_key="MY_API_SECRET")))

    def test_environment_overrides_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DEEPSEEK_API_KEY=file-key\nDEEPSEEK_MODEL=file-model\nLLM_MAX_RETRIES=1\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}, clear=True):
                config = LLMConfig.from_env(path)
            self.assertEqual(config.api_key, "environment-key")
            self.assertEqual(config.model, "file-model")
            self.assertEqual(config.max_retries, 1)

    def test_invalid_config_has_safe_error_message(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "SECRET", "LLM_TIMEOUT": "SECRET"}, clear=True):
            with self.assertRaises(ValueError) as captured:
                LLMConfig.from_env(Path("absent.env"))
        self.assertNotIn("SECRET", str(captured.exception))

    def test_missing_key_is_reported(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            LLMConfig.from_env(Path("absent.env"))

    def test_invalid_numeric_parameters(self) -> None:
        for kwargs in ({"timeout": 0}, {"timeout": float("inf")}, {"max_retries": -1}, {"max_tokens": 0}, {"temperature": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LLMConfig(api_key="test", **kwargs)
