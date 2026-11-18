"""播放进度、取消竞争和夜晚遮屏的 JavaScript 回归。"""

from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "前端回归需要 Node")
class PlaybackFrontendTests(unittest.TestCase):
    def test_playback_queue_browser_subtitles_and_night_presentation(self):
        script = r'''
import fs from "node:fs";
import assert from "node:assert/strict";
const load = async name => import("data:text/javascript;base64," + Buffer.from(fs.readFileSync(`web/static/${name}.js`, "utf8")).toString("base64"));
const { SpeechQueue, BrowserTTSAdapter, TextPlaybackAdapter, VOICE_PRESETS } = await load("speech");
const { playbackSnapshot, nightCurtain } = await load("presentation");
assert.deepEqual(VOICE_PRESETS.map(item => item.id), ["gentle_male", "gentle_female", "doubao"]);
const base = { status: "running", phase: "night", private: { role: "villager" }, events: [{ kind: "night_started" }], pending: null };
for (const role of ["villager", "seer", "witch", "hunter", "guard"]) {
  assert.equal(nightCurtain({ ...base, private: { role } }).closed, true);
}
assert.equal(nightCurtain({ ...base, private: { role: "werewolf" } }).closed, false);
assert.equal(nightCurtain({ ...base, private: null }).closed, true);
assert.equal(nightCurtain({ ...base, status: "stopped" }).closed, false);
assert.equal(nightCurtain({ ...base, phase: "day" }).closed, false);
assert.equal(nightCurtain({ ...base, pending: { action: "inspect" } }).ownAction, true);
assert.equal(nightCurtain({ ...base, phase: "hunter" }).closed, true);
const ahead = { phase: "voting", events: [{ kind: "speech" }, { kind: "voting_started" }], discussion_round: 2, playback: { player_id: 3, event_index: 0, discussion_round: 1 } };
const visible = playbackSnapshot(ahead);
assert.equal(visible.phase, "day");
assert.equal(visible.events.length, 1);
assert.equal(visible.discussion_round, 1);
assert.equal(ahead.events.length, 2);

// 清空旧语音后开始新语音，旧 Promise 延迟报错不能取消新一代队列。
const pending = [];
const calls = [];
const queue = new SpeechQueue({
  speak(text) { calls.push(text); return new Promise((resolve, reject) => pending.push({ resolve, reject })); },
  cancel() {},
}, () => {}, () => {});
const old = queue.enqueue("old", 1);
queue.clear();
assert.equal((await old).status, "canceled");
const current = queue.enqueue("current", 2);
const next = queue.enqueue("next", 3);
pending[0].reject(new Error("stale"));
await Promise.resolve();
pending[1].resolve();
assert.equal((await current).status, "finished");
await Promise.resolve();
assert.deepEqual(calls, ["old", "current", "next"]);
pending[2].resolve();
assert.equal((await next).status, "finished");

// 使用可控系统声线观察字幕进度，全部短句完成后才结束整条发言。
const utterances = [];
globalThis.window = { speechSynthesis: {
  getVoices() { return [{ name: "Microsoft Kangkang", lang: "zh-CN" }, { name: "Microsoft Huihui", lang: "zh-CN" }]; },
  cancel() {},
  speak(utterance) {
    utterances.push(utterance);
    utterance.onstart();
    setTimeout(() => { utterance.onboundary({ charIndex: 1, charLength: 1 }); utterance.onend(); }, 5);
  },
} };
globalThis.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
const browser = new BrowserTTSAdapter();
const progress = [];
const speech = "你好🙂，请你回应一下我前面提出的具体问题。";
await browser.speak(speech, { preset: "gentle_male", onProgress: count => progress.push(count) });
assert.equal(progress.at(-1), speech.length);
assert.ok(progress[0] < speech.length);
assert.ok(progress.every((count, index) => index === 0 || count >= progress[index - 1]));
assert.ok(progress.every(count => !/[\uD800-\uDBFF]$/.test(speech.slice(0, count))));
assert.equal(utterances[0].voice.name, "Microsoft Kangkang");
assert.equal(utterances[0].rate, 1.3);
assert.equal(utterances.length, 1);
assert.equal(utterances.map(item => item.text).join(""), speech);
await browser.speak("你好。", { preset: "gentle_female", rate: 1.6 });
assert.equal(utterances.at(-1).rate, 1.6);
assert.equal(utterances.at(-1).voice.name, "Microsoft Huihui");
const text = new TextPlaybackAdapter();
const quiet = [];
await text.speak("你好", { onProgress: count => quiet.push(count) });
assert.deepEqual(quiet, [1, 2]);
console.log("playback frontend regression passed");
'''
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
