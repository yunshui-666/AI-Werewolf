"""前端源码与语音队列回归。

运行网页本身无需 Node。检测到 Node 时，用假语音适配器验证串行播放和
错误提示；未安装 Node 的 Python 环境会明确跳过这些可选开发测试。
"""

from pathlib import Path
import shutil
import subprocess
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "语音队列开发测试需要 Node，可选")
class SpeechQueueTests(unittest.TestCase):
    """以真实 JS 模块和 Promise 执行顺序检查语音输出协议。"""

    def test_speech_queue_is_serial_and_errors_remain_visible(self):
        script = r'''
import fs from "node:fs";
import assert from "node:assert/strict";
const source = fs.readFileSync("web/static/speech.js", "utf8");
const { SpeechQueue } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
let active = 0;
let maximum = 0;
const calls = [];
const statuses = [];
const adapter = {
  async speak(text) {
    active += 1;
    maximum = Math.max(maximum, active);
    calls.push(text);
    await new Promise(resolve => setTimeout(resolve, 5));
    active -= 1;
  },
  cancel() {}
};
const queue = new SpeechQueue(adapter, text => statuses.push(text), () => {});
queue.enqueue("first", 1);
queue.enqueue("second", 2);
await new Promise(resolve => setTimeout(resolve, 40));
assert.deepEqual(calls, ["first", "second"]);
assert.equal(maximum, 1);
const failedStatuses = [];
const failed = new SpeechQueue({ async speak() { throw new Error("voice unavailable"); }, cancel() {} }, text => failedStatuses.push(text), () => {});
failed.enqueue("test", 1);
await new Promise(resolve => setTimeout(resolve, 10));
assert.equal(failedStatuses.at(-1), "voice unavailable");
console.log("speech queue tests passed");
'''
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], cwd=PROJECT_ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_modules_parse(self):
        for name in ("app.js", "speech.js", "presentation.js"):
            with self.subTest(name=name):
                result = subprocess.run(
                    ["node", "--check", str(PROJECT_ROOT / "web" / "static" / name)],
                    capture_output=True, text=True, encoding="utf-8", timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
