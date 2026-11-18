"""仅监听本机的标准库 HTTP 服务。

静态页面与 API 同源，Python 后台保存规则状态和 Key。页面以短轮询接收
玩家白名单快照。服务无需数据库、Node 构建或额外 Web 框架。
"""

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
import webbrowser

from pydantic import ValidationError

from agents.skills import SUPPORTED_ROLES, load_role_skill
from config import PRESETS
from game.engine import ROLE_NAMES
from game.state import Role
from llm.profiles import APIProfile, ProfileRegistry
from web.session import GameSession, SessionOptions
from web.tts import TTSService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"


class LocalGameServer(ThreadingHTTPServer):
    """服务实例持有当前会话，避免 handler 类变量造成测试间共享。"""

    daemon_threads = True

    def __init__(self, address, project_root: Path = PROJECT_ROOT):
        """为此 HTTP 服务创建配置注册表、会话引用和管理锁，随后初始化标准库服务器。"""
        self.registry = ProfileRegistry(project_root)
        self.tts = TTSService(project_root)
        self.session: GameSession | None = None
        self.session_lock = Lock()
        super().__init__(address, GameRequestHandler)

    def server_close(self):
        """服务关闭会唤醒真人输入等待，后台任务在自身边界退出。"""
        if self.session:
            self.session.stop()
        super().server_close()


class GameRequestHandler(BaseHTTPRequestHandler):
    """HTTP 边界只接收结构化输入，输出经过白名单过滤的数据。"""

    server: LocalGameServer

    def log_message(self, format, *args):
        """禁用默认请求行日志，避免会话 token 出现在控制台中。"""

    def _send_json(self, value, status=200):
        """统一编码与缓存策略，错误响应也保持 JSON 格式。"""
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _session(self, session_id):
        """只有当前开局返回的随机会话 ID 能读取本人私有面板或提交行动。"""
        session = self.server.session
        if session is None or not isinstance(session_id, str) or session.session_id != session_id:
            raise ValueError("当前游戏不存在或已结束，请重新开局")
        return session

    def _local_host(self):
        """Host 也限制为回环主机，避免外部域名解析变化后进入本机配置接口。"""
        try:
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
        except ValueError:
            return False
        return host in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self):
        """提供静态资源、非敏感配置以及当前玩家快照。"""
        if not self._local_host():
            return self._send_json({"error": "仅接受本机主机名"}, 403)
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/health":
                return self._send_json({"ok": True})
            if parsed.path == "/api/setup":
                query = parse_qs(parsed.query)
                try:
                    options = SessionOptions.model_validate({
                        "preset": query.get("preset", ["standard12"])[0],
                        "human_id": int(query.get("human_id", ["1"])[0]),
                        "selections": json.loads(query.get("selections", ["{}"])[0]),
                    })
                except (ValueError, TypeError):
                    return self._send_json({"error": "座位接口预览参数无效"}, 400)
                return self._send_json({
                    "presets": PRESETS, "profiles": self.server.registry.public_profiles(),
                    **self.server.registry.routing_setup(
                        options.game_config().player_count, options.human_id,
                        {int(pid): profile for pid, profile in options.selections.items()},
                    ),
                    "role_names": {role.value: name for role, name in ROLE_NAMES.items()},
                    "voice": self.server.tts.public_config(),
                })
            if parsed.path == "/api/skills":
                return self._send_json({role: {
                    "name": load_role_skill(role).name, "instructions": load_role_skill(role).instructions,
                } for role in sorted(SUPPORTED_ROLES)})
            if parsed.path == "/api/game":
                sid = parse_qs(parsed.query).get("session", [None])[0]
                return self._send_json(self._session(sid).snapshot())
            if parsed.path == "/api/research":
                return self._send_json({"markdown": (PROJECT_ROOT / "docs" / "RESEARCH.md").read_text(encoding="utf-8-sig")})
            relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            target = (STATIC_ROOT / relative).resolve()
            if not target.is_relative_to(STATIC_ROOT.resolve()) or not target.is_file():
                return self._send_json({"error": "页面不存在"}, 404)
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 404)

    def do_POST(self):
        """同源请求检查、JSON 校验和业务操作都在一个明确的入口执行。"""
        if not self._local_host():
            return self._send_json({"error": "仅接受本机主机名"}, 403)
        origin = self.headers.get("Origin")
        expected_origin = f"http://{self.headers.get('Host')}"
        if origin is not None and origin != expected_origin:
            return self._send_json({"error": "请求来源不匹配"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 131072:
                raise ValueError("请求体大小无效")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            route = urlsplit(self.path).path
            if route == "/api/profiles":
                profile_data = body.get("profile")
                key = body.get("api_key", "")
                if not isinstance(key, str):
                    raise ValueError("API Key 格式无效")
                self.server.registry.save(APIProfile.model_validate(profile_data), key)
                return self._send_json({"profiles": self.server.registry.public_profiles()})
            if route == "/api/game/start":
                options = SessionOptions.model_validate(body)
                with self.server.session_lock:
                    previous = self.server.session
                    if previous and previous.thread.is_alive():
                        raise ValueError("请先结束当前对局，等待正在执行的动作返回")
                    session = GameSession(options, self.server.registry)
                    self.server.session = session
                    session.start()
                return self._send_json(session.snapshot(), 201)
            session = self._session(body.get("session_id"))
            if route == "/api/tts":
                text = session.speech_text(body.get("event_index"))
                return self._send_json(self.server.tts.synthesize(text, rate=body.get("rate", 1.3)))
            if route == "/api/game/playback":
                token = body.get("playback_id")
                if not isinstance(token, str):
                    raise ValueError("播放确认格式无效")
                session.acknowledge_playback(token)
                return self._send_json({"ok": True})
            if route == "/api/game/action":
                session.submit(body.get("request_id"), body.get("decision"))
                return self._send_json({"ok": True})
            if route == "/api/game/stop":
                session.stop()
                return self._send_json({"ok": True})
            return self._send_json({"error": "接口不存在"}, 404)
        except (ValidationError, TypeError, KeyError):
            self._send_json({"error": "输入格式或行动无效，请核对当前目标、药品和必填项。"}, 400)
        except (ValueError, json.JSONDecodeError) as exc:
            # 只转发本层固定的业务提示；JSON 解析错误使用统一描述。
            message = "JSON 格式错误" if isinstance(exc, json.JSONDecodeError) else str(exc)
            self._send_json({"error": message}, 400)
        except Exception:
            self._send_json({"error": "本地服务处理失败，请检查接口配置和目录权限。"}, 500)


def main():
    """启动网页服务。默认绑定回环地址，终端 Ctrl+C 可完整关闭。"""
    parser = argparse.ArgumentParser(description="WerewolfArena 本地圆桌界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="仅启动服务，手动打开地址")
    args = parser.parse_args()
    try:
        server = LocalGameServer(("127.0.0.1", args.port))
    except (ValueError, OSError, OverflowError):
        parser.error("启动失败，请检查端口、providers.json 配置和目录权限。")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"WerewolfArena 已启动，请打开 {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("本地游戏服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
