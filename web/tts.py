"""可选豆包语音合成。凭据保留在 Python，浏览器只接收公开发言的音频与字幕。"""

import base64
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
import httpx


@dataclass(frozen=True)
class DoubaoConfig:
    """支持新版 API Key 与旧版 App ID / Access Token，敏感字段不进入 repr。"""

    api_key: str = field(default="", repr=False)
    app_id: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    resource_id: str = "seed-tts-2.0"
    speaker: str = "zh_female_vv_uranus_bigtts"

    @classmethod
    def from_env(cls, root: Path) -> "DoubaoConfig":
        """每次取用时读取，用户补填 .env 后下一次播放即可使用。"""
        values = {**dotenv_values(root / ".env", encoding="utf-8-sig"), **os.environ}
        return cls(**{
            field_name: (values.get(variable) or default).strip()
            for field_name, variable, default in (
                ("api_key", "DOUBAO_TTS_API_KEY", ""),
                ("app_id", "DOUBAO_TTS_APP_ID", ""),
                ("access_token", "DOUBAO_TTS_ACCESS_TOKEN", ""),
                ("resource_id", "DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0"),
                ("speaker", "DOUBAO_TTS_SPEAKER", "zh_female_vv_uranus_bigtts"),
            )
        })

    @property
    def ready(self) -> bool:
        """仅表示所需凭据已填写，权限和额度由实际合成时确认。"""
        return bool((self.api_key or self.app_id and self.access_token) and self.resource_id and self.speaker)


class TTSService:
    """按需调用官方合成接口，默认本机声线的使用不经过该服务。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def public_config(self) -> dict:
        return {"doubao_ready": DoubaoConfig.from_env(self.root).ready}

    def synthesize(self, text: str, rate: float = 1.3) -> dict:
        """组合流式音频分片并整理字级字幕，任何服务异常均返回固定业务提示。"""
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0.8 <= rate <= 2:
            raise ValueError("语速必须在 0.8 到 2.0 之间")
        config = DoubaoConfig.from_env(self.root)
        if not config.ready:
            raise ValueError("豆包声色尚未配置，请填写 DOUBAO_TTS_API_KEY 或旧版 App ID 与 Access Token")
        headers = {
            "X-Api-Resource-Id": config.resource_id,
            "X-Api-Request-Id": uuid4().hex,
        }
        if config.api_key:
            headers["X-Api-Key"] = config.api_key
        else:
            headers["X-Api-App-Id"] = config.app_id
            headers["X-Api-Access-Key"] = config.access_token
        body = {"req_params": {
            "text": text, "speaker": config.speaker,
            "audio_params": {"format": "mp3", "sample_rate": 24000, "enable_subtitle": True, "speech_rate": round((rate - 1) * 100)},
        }}
        audio = bytearray()
        words = []
        seen = set()
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                with client.stream("POST", "https://openspeech.bytedance.com/api/v3/tts/unidirectional", headers=headers, json=body) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        if item.get("code") not in {0, 20000000}:
                            raise ValueError("语音服务返回错误")
                        if item.get("data"):
                            audio.extend(base64.b64decode(item["data"], validate=True))
                        if len(audio) > 8 * 1024 * 1024:
                            raise ValueError("音频响应过大")
                        for word in (item.get("sentence") or {}).get("words", []):
                            key = (word["startTime"], word["endTime"], word["word"])
                            if key not in seen:
                                seen.add(key)
                                words.append(word)
            if not audio:
                raise ValueError("语音数据为空")
            # 服务字幕按时间排序，匹配原文后转换为 JS 的 UTF-16 字符下标。
            subtitles = []
            cursor = 0
            for word in sorted(words, key=lambda value: value["startTime"]):
                start = float(word["startTime"])
                if not math.isfinite(start) or start < 0 or not word["word"]:
                    continue
                index = text.find(word["word"], cursor)
                if index < 0:
                    continue
                cursor = index + len(word["word"])
                subtitles.append({"start": start, "end_index": len(text[:cursor].encode("utf-16-le")) // 2})
            return {"audio": base64.b64encode(audio).decode("ascii"), "subtitles": subtitles}
        except Exception:
            # HTTP 异常及服务 message 可能含凭据和内部信息，不向浏览器或日志回显。
            raise ValueError("豆包语音合成失败，请检查凭据、音色权限、额度及网络") from None
