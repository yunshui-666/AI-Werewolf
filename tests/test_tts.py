"""可选豆包服务完全离线测试，所有请求使用本地 HTTP 模拟传输。"""

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from web.tts import DoubaoConfig, TTSService


class TTSServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = TTSService(self.root)

    def configure(self, text="DOUBAO_TTS_API_KEY=SECRET_MARKER\n"):
        (self.root / ".env").write_text(text, encoding="utf-8")

    def test_optional_service_is_unconfigured_by_default(self):
        self.assertEqual(self.service.public_config(), {"doubao_ready": False})
        with self.assertRaises(ValueError):
            self.service.synthesize("你好")

    def test_public_config_and_repr_do_not_expose_credentials(self):
        self.configure()
        self.assertTrue(self.service.public_config()["doubao_ready"])
        self.assertNotIn("SECRET_MARKER", repr(DoubaoConfig.from_env(self.root)))
        self.assertNotIn("SECRET_MARKER", json.dumps(self.service.public_config()))

    def test_audio_chunks_and_word_timestamps_are_combined(self):
        self.configure()
        requests = []
        chunks = [
            {"code": 0, "data": base64.b64encode(b"first").decode(), "sentence": {"words": [{"word": "你", "startTime": 0.1, "endTime": 0.3}]}},
            {"code": 0, "data": base64.b64encode(b"second").decode(), "sentence": {"words": [{"word": "🙂", "startTime": 0.4, "endTime": 0.6}]}},
            {"code": 20000000},
        ]
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text="\n".join(json.dumps(item) for item in chunks))
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch("web.tts.httpx.Client", return_value=client):
            result = self.service.synthesize("你🙂好")
        self.assertEqual(base64.b64decode(result["audio"]), b"firstsecond")
        self.assertEqual(result["subtitles"], [{"start": 0.1, "end_index": 1}, {"start": 0.4, "end_index": 3}])
        self.assertEqual(requests[0].headers["X-Api-Key"], "SECRET_MARKER")
        self.assertEqual(requests[0].url.host, "openspeech.bytedance.com")
        payload = json.loads(requests[0].content)
        self.assertTrue(payload["req_params"]["audio_params"]["enable_subtitle"])
        self.assertEqual(payload["req_params"]["audio_params"]["speech_rate"], 30)

    def test_legacy_credentials_are_supported(self):
        self.configure("DOUBAO_TTS_APP_ID=legacy-app\nDOUBAO_TTS_ACCESS_TOKEN=SECRET_MARKER\n")
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text=json.dumps({"code": 0, "data": "YWJj"}))
        with patch("web.tts.httpx.Client", return_value=httpx.Client(transport=httpx.MockTransport(handler))):
            self.service.synthesize("你好")
        self.assertEqual(requests[0].headers["X-Api-App-Id"], "legacy-app")
        self.assertEqual(requests[0].headers["X-Api-Access-Key"], "SECRET_MARKER")
        self.assertNotIn("X-Api-Key", requests[0].headers)

    def test_service_errors_are_sanitized(self):
        self.configure()
        responses = [
            httpx.Response(401, text="SECRET_MARKER"),
            httpx.Response(200, text=json.dumps({"code": 123, "message": "SECRET_MARKER"})),
            httpx.Response(200, text=json.dumps({"code": 0})),
            httpx.Response(200, text="broken-json"),
        ]
        for response in responses:
            with self.subTest(response=response.status_code):
                client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
                with patch("web.tts.httpx.Client", return_value=client), self.assertRaises(ValueError) as captured:
                    self.service.synthesize("你好")
                self.assertNotIn("SECRET_MARKER", str(captured.exception))
