"""通过真实本机 HTTP 服务验证网页流程，所有 AI 均使用离线策略。

测试覆盖网页提交与游戏线程之间的等待/唤醒、请求去重、隐私白名单及接口
配置。模型配置测试用假客户端替代网络，避免访问用户已填写的 API。
"""

import http.client
import json
import logging
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from agents.player import MockAgent
from config import GameConfig
from game.engine import GameEngine
from game.state import Role
from llm.profiles import APIProfile, ProfileRegistry
from web.server import LocalGameServer


class HTTPGameTests(unittest.TestCase):
    """每项用例使用独立临时配置目录和随机可用端口。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = LocalGameServer(("127.0.0.1", 0), self.root)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.server.session:
            self.server.session.thread.join(timeout=3)
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, origin=None):
        """真实 HTTP 序列化与状态码验证，比直接调用 handler 更接近浏览器。"""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, content

    def json_request(self, method, path, body=None, origin=None):
        status, content = self.request(method, path, body, origin)
        return status, json.loads(content)

    def start(self, human=1, preset="standard12", playback=False):
        status, snapshot = self.json_request("POST", "/api/game/start", {
            "preset": preset, "human_id": human, "seed": 42, "playback_sync": playback,
            "delay_ms": 0, "max_rounds": 4, "discussion_rounds": 2,
            "selections": {str(pid): "mock" for pid in range(1, 13 if preset == "standard12" else 9)},
        })
        self.assertEqual(status, 201, snapshot)
        return snapshot

    def wait_for(self, sid, predicate):
        """有界轮询避免测试挂起，超时会给出最后一次可见快照。"""
        deadline = time.monotonic() + 10
        snapshot = None
        while time.monotonic() < deadline:
            status, snapshot = self.json_request("GET", f"/api/game?session={sid}")
            self.assertEqual(status, 200)
            if predicate(snapshot):
                return snapshot
            time.sleep(0.01)
        self.fail(f"等待游戏状态超时 {snapshot and snapshot['status']}")

    def test_static_page_and_setup(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("圆桌记录", page.decode())
        _, setup = self.json_request("GET", "/api/setup")
        self.assertEqual(sum(setup["presets"]["standard12"].values()), 12)
        self.assertEqual(setup["profiles"][0]["profile_id"], "mock")

    def test_setup_previews_deepseek_pool_for_selected_human(self):
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=fake-one\nDEEPSEEK_API2=fake-two\n", encoding="utf-8")
        _, setup = self.json_request("GET", "/api/setup?preset=classic8&human_id=3")
        self.assertEqual(setup["deepseek_pool"], ["deepseek", "deepseek_2"])
        self.assertEqual(setup["seat_defaults"], {
            "1": "deepseek", "2": "deepseek_2", "3": "mock", "4": "deepseek", "5": "deepseek_2", "6": "deepseek", "7": "deepseek_2", "8": "deepseek",
        })
        self.assertNotIn("fake-one", json.dumps(setup))
        self.assertNotIn("fake-two", json.dumps(setup))
        status, _ = self.json_request("GET", "/api/setup?preset=classic8&human_id=12")
        self.assertEqual(status, 400)

    def test_skill_reference_has_six_roles(self):
        _, skills = self.json_request("GET", "/api/skills")
        self.assertEqual(set(skills), {role.value for role in Role})
        self.assertIn("连续", skills["guard"]["instructions"])

    def test_hidden_roles_and_other_private_memories_are_absent(self):
        snapshot = self.start()
        snapshot = self.wait_for(snapshot["session_id"], lambda value: value["pending"] is not None)
        self.assertEqual(snapshot["private"]["role"], "guard")
        for player in snapshot["players"]:
            if not player["human"]:
                self.assertIsNone(player["role"])
                self.assertNotIn("memory", player)
        self.assertNotIn("wolf_choices", snapshot)
        self.assertNotIn("death_causes", snapshot)
        self.assertNotIn("api_key", json.dumps(snapshot))

    def test_invalid_and_duplicate_actions_are_rejected(self):
        snapshot = self.start()
        sid = snapshot["session_id"]
        snapshot = self.wait_for(sid, lambda value: value["pending"] is not None)
        pending = snapshot["pending"]
        body = {"session_id": sid, "request_id": pending["request_id"], "decision": {"target_id": 999}}
        status, _ = self.json_request("POST", "/api/game/action", body)
        self.assertEqual(status, 400)
        body["decision"] = {"target_id": None}
        status, _ = self.json_request("POST", "/api/game/action", body)
        self.assertEqual(status, 200)
        status, _ = self.json_request("POST", "/api/game/action", body)
        self.assertEqual(status, 400)

    def test_session_token_and_origin_are_checked(self):
        self.start()
        status, _ = self.json_request("GET", "/api/game?session=wrong")
        self.assertEqual(status, 404)
        status, _ = self.json_request("POST", "/api/game/start", {}, origin="https://different.example")
        self.assertEqual(status, 403)

    def test_external_host_header_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", "/api/setup", headers={"Host": "external.example"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        connection.close()

    def test_stop_wakes_human_input(self):
        snapshot = self.start()
        sid = snapshot["session_id"]
        self.wait_for(sid, lambda value: value["pending"] is not None)
        self.json_request("POST", "/api/game/stop", {"session_id": sid})
        result = self.wait_for(sid, lambda value: value["status"] == "stopped")
        self.assertIsNone(result["pending"])

    def test_profile_secret_is_only_kept_in_memory(self):
        status, body = self.json_request("POST", "/api/profiles", {
            "profile": {"profile_id": "sample", "name": "示例", "base_url": "https://api.example.com/v1", "model": "example"},
            "api_key": "OFFLINE_SECRET_MARKER",
        })
        self.assertEqual(status, 200)
        self.assertNotIn("OFFLINE_SECRET_MARKER", json.dumps(body))
        self.assertNotIn("OFFLINE_SECRET_MARKER", (self.root / "providers.json").read_text())
        self.assertTrue(next(item for item in body["profiles"] if item["profile_id"] == "sample")["has_key"])

    def test_spectator_game_reveals_all_roles_only_at_finish(self):
        snapshot = self.start(human=0)
        result = self.wait_for(snapshot["session_id"], lambda value: value["status"] == "finished")
        self.assertIsNone(result["private"])
        self.assertTrue(all(player["role"] for player in result["players"]))
        self.assertEqual(result["fallback_count"], 0)

    def test_humans_can_finish_all_six_roles_through_http(self):
        fixture = GameEngine(GameConfig(preset="standard12", seed=42))
        for role in Role:
            with self.subTest(role=role):
                human = next(pid for pid, player in fixture.state.players.items() if player.role is role)
                snapshot = self.start(human=human)
                sid = snapshot["session_id"]
                deadline = time.monotonic() + 15
                last_request = None
                while time.monotonic() < deadline:
                    _, snapshot = self.json_request("GET", f"/api/game?session={sid}")
                    if snapshot["status"] == "finished":
                        break
                    self.assertNotEqual(snapshot["status"], "error", snapshot["error"])
                    pending = snapshot["pending"]
                    if pending and pending["request_id"] != last_request:
                        action = pending["action"]
                        target = pending["candidates"][0] if pending["candidates"] else None
                        if action in {"guard", "witch", "hunter_shoot", "speak", "wolf_discuss"}:
                            target = None
                        status, result = self.json_request("POST", "/api/game/action", {
                            "session_id": sid, "request_id": pending["request_id"],
                            "decision": {"target_id": target, "text": "我会根据公开记录比较各位的发言。"},
                        })
                        self.assertEqual(status, 200, result)
                        last_request = pending["request_id"]
                    time.sleep(0.005)
                self.assertEqual(snapshot["status"], "finished")
                self.assertEqual(snapshot["fallback_count"], 0)
                self.server.session.thread.join(timeout=2)

    def test_playback_waits_for_ack_and_stop_wakes_it(self):
        snapshot = self.start(human=0, preset="classic8", playback=True)
        sid = snapshot["session_id"]
        first = self.wait_for(sid, lambda value: value["playback"] is not None)
        token = first["playback"]["playback_id"]
        self.assertEqual(sum(event["kind"] == "speech" for event in first["events"]), 1)
        time.sleep(0.03)
        _, still = self.json_request("GET", f"/api/game?session={sid}")
        self.assertEqual(still["playback"]["playback_id"], token)
        self.assertEqual(len(still["events"]), len(first["events"]))
        status, _ = self.json_request("POST", "/api/game/playback", {"session_id": sid, "playback_id": "expired"})
        self.assertEqual(status, 400)
        for _ in range(2):
            status, _ = self.json_request("POST", "/api/game/playback", {"session_id": sid, "playback_id": token})
            self.assertEqual(status, 200)
        second = self.wait_for(sid, lambda value: value["playback"] and value["playback"]["playback_id"] != token)
        self.assertEqual(sum(event["kind"] == "speech" for event in second["events"]), 2)
        self.json_request("POST", "/api/game/stop", {"session_id": sid})
        stopped = self.wait_for(sid, lambda value: value["status"] == "stopped")
        self.assertIsNone(stopped["playback"])

    def test_complete_game_with_playback_acknowledgments(self):
        snapshot = self.start(human=0, preset="classic8", playback=True)
        sid = snapshot["session_id"]
        seen = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, snapshot = self.json_request("GET", f"/api/game?session={sid}")
            if snapshot["status"] == "finished":
                break
            self.assertNotEqual(snapshot["status"], "error", snapshot["error"])
            playback = snapshot["playback"]
            if playback and playback["playback_id"] not in seen:
                self.assertIsNone(snapshot["pending"])
                event = snapshot["events"][playback["event_index"]]
                self.assertEqual(event["kind"], "speech")
                self.assertEqual(event["actor_id"], playback["player_id"])
                status, _ = self.json_request("POST", "/api/game/playback", {"session_id": sid, "playback_id": playback["playback_id"]})
                self.assertEqual(status, 200)
                seen.add(playback["playback_id"])
            time.sleep(0.005)
        self.assertEqual(snapshot["status"], "finished")
        self.assertEqual(snapshot["fallback_count"], 0)
        self.assertGreater(len(seen), 5)

    def test_model_warning_is_saved_in_current_game_log(self):
        def run_with_warning(runner):
            logging.getLogger("werewolf_arena.public").warning("模型调用失败（HTTP 429，请求频率或用量达到限制）。")
            return runner.run()
        with patch("web.session.run_graph", side_effect=run_with_warning):
            snapshot = self.start(human=0, preset="classic8")
            result = self.wait_for(snapshot["session_id"], lambda value: value["status"] == "finished")
            self.server.session.thread.join(timeout=2)
        content = (self.root / "logs" / result["log_file"]).read_text(encoding="utf-8")
        self.assertIn("HTTP 429", content)
        self.assertNotIn("SessionWarningHandler", str(logging.getLogger("werewolf_arena.public").handlers))

    def test_tts_only_receives_public_speech_with_valid_session(self):
        snapshot = self.start(human=0, preset="classic8")
        sid = snapshot["session_id"]
        result = self.wait_for(sid, lambda value: value["status"] == "finished")
        index = next(i for i, event in enumerate(result["events"]) if event["kind"] == "speech")
        with patch.object(self.server.tts, "synthesize", return_value={"audio": "AA==", "subtitles": []}) as synthesize:
            for body in ({"session_id": "wrong", "event_index": index}, {"session_id": sid, "event_index": 0}):
                status, _ = self.json_request("POST", "/api/tts", body)
                self.assertEqual(status, 400)
            synthesize.assert_not_called()
            status, _ = self.json_request("POST", "/api/tts", {"session_id": sid, "event_index": index, "text": "PRIVATE_INPUT"})
            self.assertEqual(status, 200)
            synthesize.assert_called_once_with(result["events"][index]["text"].split("：", 1)[-1], rate=1.3)


class ProfileRegistryTests(unittest.TestCase):
    """不同座位的 API 选择需要得到不同客户端，个人上下文继续单独构造。"""

    def test_separate_profiles_and_shared_connection_for_same_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ProfileRegistry(Path(directory))
            for name in ("one", "two"):
                registry.save(APIProfile(profile_id=name, name=name, base_url="https://api.example.com/v1", model=name), "fake")
            with patch("llm.profiles.DeepSeekClient") as factory:
                factory.side_effect = lambda config, pacer=None: type("FakeClient", (), {"config": config, "pacer": pacer, "close": lambda self: None})()
                agents, clients = registry.create_agents(4, {1: "mock", 2: "one", 3: "two", 4: "one"}, 42)
            self.assertIsInstance(agents[1], MockAgent)
            self.assertIs(agents[2].client, agents[4].client)
            self.assertIsNot(agents[2].client, agents[3].client)
            self.assertEqual(len(clients), 2)
            self.assertEqual(agents[3].client.config.model, "two")
