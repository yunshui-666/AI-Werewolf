// 语速统一用于本机、逐字文字和可选云语音，默认比旧版更快。
export const DEFAULT_SPEECH_RATE = 1.3;
function speechRate(value = DEFAULT_SPEECH_RATE) {
  return Number.isFinite(value) ? Math.max(0.8, Math.min(2, value)) : DEFAULT_SPEECH_RATE;
}

/** 只保留三个产品级声线入口，底层系统声线由适配器自动选择。 */
export const VOICE_PRESETS = [
  { id: "gentle_male", name: "温柔男声" },
  { id: "gentle_female", name: "温柔女声" },
  { id: "doubao", name: "豆包声色" },
];

export function defaultVoice(playerId) {
  return playerId % 2 === 0 ? "gentle_female" : "gentle_male";
}

// charIndex 使用 UTF-16 下标，截断时避免只显示一个表情字符的半个代理对。
function safeLength(text, length) {
  const count = Math.max(0, Math.min(text.length, length));
  const code = text.charCodeAt(count - 1);
  return code >= 0xd800 && code <= 0xdbff ? count - 1 : count;
}

/** 本机语音通过边界事件校准字幕，缺少边界事件时在短句内按语速平滑推进。 */
export class BrowserTTSAdapter {
  voices() {
    return "speechSynthesis" in window ? window.speechSynthesis.getVoices() : [];
  }
  async speak(text, { preset = "gentle_female", rate = DEFAULT_SPEECH_RATE, onProgress = () => {} } = {}) {
    if (!("speechSynthesis" in window)) throw new Error("当前浏览器未提供本机语音，可关闭语音或跳过本条");
    const chinese = this.voices().filter((voice) => /^zh/i.test(voice.lang));
    const preferred = preset === "gentle_male" ? /Kangkang|Yunxi|Yunyang|男/i : /Huihui|Yaoyao|Xiaoxiao|女/i;
    const voice = chinese.find((item) => preferred.test(item.name)) || chinese[0];
    if (!voice) throw new Error("本机缺少中文声线，请安装中文系统语音或选择已配置的豆包声色");
    rate = speechRate(rate);
    const run = { canceled: false, finish: null };
    this.current = run;
    // 按完整句子播放，较长句子最多 80 字，减少频繁重启系统语音造成的停顿。
    const chunks = [...text.matchAll(/[^。！？!?\n]{1,80}[。！？!?\n]*|[。！？!?\n]+/gu)];
    try {
      for (const match of chunks) {
        if (run.canceled) break;
        const chunk = match[0];
        const offset = match.index;
        await new Promise((resolve, reject) => {
          const utterance = new SpeechSynthesisUtterance(chunk);
          utterance.lang = voice.lang || "zh-CN";
          utterance.voice = voice;
          utterance.rate = rate;
          utterance.pitch = preset === "gentle_male" ? 0.9 : 1.05;
          let timer = null;
          let watchdog = null;
          let shown = 0;
          let settled = false;
          let lastBoundary = 0;
          const progress = (count) => {
            if (run.canceled) return;
            shown = Math.max(shown, safeLength(chunk, count));
            onProgress(offset + shown);
          };
          const finish = (error) => {
            if (settled) return;
            settled = true;
            clearInterval(timer);
            clearTimeout(watchdog);
            if (error) window.speechSynthesis.cancel();
            if (!run.canceled && !error) onProgress(offset + chunk.length);
            run.finish = null;
            if (error) reject(error);
            else resolve();
          };
          run.finish = () => finish();
          utterance.onstart = () => {
            const started = performance.now();
            progress(1);
            timer = setInterval(() => {
              if (lastBoundary && performance.now() - lastBoundary < 350) return;
              const count = Math.floor((performance.now() - started) * rate / 210) + 1;
              progress(Math.min(chunk.length - 1, count));
            }, 70);
          };
          utterance.onboundary = (event) => {
            lastBoundary = performance.now();
            progress(Math.min(chunk.length - 1, event.charIndex + (event.charLength || 1)));
          };
          utterance.onend = () => finish();
          utterance.onerror = () => finish(run.canceled
            ? null : new Error("本机语音播放失败，可重试语音或跳过本条"));
          watchdog = setTimeout(() => finish(new Error("语音服务未完成播放，可重试语音或跳过本条")), Math.max(20000, chunk.length * 600 / rate + 10000));
          window.speechSynthesis.speak(utterance);
        });
      }
      if (!run.canceled) onProgress(text.length);
    } finally {
      if (this.current === run) this.current = null;
    }
  }
  cancel() {
    const run = this.current;
    if (run) run.canceled = true;
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    run?.finish?.();
  }
}

/** 关闭声音时仍逐字显示，流程使用相同的完成确认边界。 */
export class TextPlaybackAdapter {
  speak(text, { rate = DEFAULT_SPEECH_RATE, onProgress = () => {} } = {}) {
    rate = speechRate(rate);
    return new Promise((resolve) => {
      let count = 0;
      const finish = () => { clearInterval(this.timer); this.finish = null; resolve(); };
      this.finish = finish;
      this.timer = setInterval(() => {
        count += 1;
        onProgress(safeLength(text, count));
        if (count >= text.length) finish();
      }, 100 / rate);
    });
  }
  cancel() { this.finish?.(); }
}

/** 豆包合成经本机后端调用，浏览器只接收当前公开发言的音频与字幕时间戳。 */
export class DoubaoTTSAdapter {
  async speak(text, { sessionId, eventIndex, rate = DEFAULT_SPEECH_RATE, onProgress = () => {} } = {}) {
    const run = { controller: new AbortController(), source: null, finish: null };
    this.current = run;
    try {
      const response = await fetch("/api/tts", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: run.controller.signal,
        body: JSON.stringify({ session_id: sessionId, event_index: eventIndex, rate: speechRate(rate) }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "豆包语音合成失败");
      if (run.controller.signal.aborted) return;
      const context = this.context || new (window.AudioContext || window.webkitAudioContext)();
      this.context = context;
      await context.resume();
      if (context.state !== "running") throw new Error("浏览器暂停了音频，请点击重试语音");
      const bytes = Uint8Array.from(atob(data.audio), (character) => character.charCodeAt(0));
      const buffer = await context.decodeAudioData(bytes.buffer);
      if (run.controller.signal.aborted) return;
      await new Promise((resolve) => {
        const source = context.createBufferSource();
        run.source = source;
        source.buffer = buffer;
        source.connect(context.destination);
        const started = context.currentTime;
        let shown = 0;
        const timer = setInterval(() => {
          const elapsed = context.currentTime - started;
          const reached = data.subtitles.filter((word) => word.start <= elapsed);
          const count = data.subtitles.length ? (reached.at(-1)?.end_index || 0)
            : Math.min(text.length - 1, Math.floor(text.length * elapsed / buffer.duration));
          shown = Math.max(shown, safeLength(text, count));
          onProgress(shown);
        }, 60);
        let settled = false;
        const finish = () => {
          if (settled) return;
          settled = true;
          clearInterval(timer);
          run.finish = null;
          source.disconnect();
          resolve();
        };
        run.finish = finish;
        source.onended = finish;
        source.start();
      });
      if (!run.controller.signal.aborted) onProgress(text.length);
    } catch (error) {
      if (!run.controller.signal.aborted) throw error;
    } finally {
      if (this.current === run) this.current = null;
    }
  }
  cancel() {
    const run = this.current;
    run?.controller.abort();
    if (run?.source) run.source.stop();
    run?.finish?.();
  }
}

/** 根据简化声线和语音开关路由。同一时刻只有队列当前项拥有播放器。 */
export class VoiceAdapter {
  constructor() {
    this.browser = new BrowserTTSAdapter();
    this.text = new TextPlaybackAdapter();
    this.doubao = new DoubaoTTSAdapter();
  }
  unlock(preset) {
    // 必须在开始/重试按钮的用户手势内恢复 WebAudio，合成完成后即可播放。
    if ("speechSynthesis" in window) window.speechSynthesis.resume();
    const Context = window.AudioContext || window.webkitAudioContext;
    if (preset === "doubao" && Context) {
      this.doubao.context ||= new Context();
      this.doubao.context.resume().catch(() => {});
    }
  }
  speak(text, options = {}) {
    this.active = options.silent ? this.text : options.preset === "doubao" ? this.doubao : this.browser;
    return this.active.speak(text, options);
  }
  cancel() { this.active?.cancel(); }
}

/** 公共发言依次播放，返回明确结果供页面确认后端，清空队列也会结算所有 Promise。 */
export class SpeechQueue {
  constructor(adapter, onStatus, onSpeaker) {
    Object.assign(this, { adapter, onStatus, onSpeaker });
    this.queue = [];
    this.running = false;
    this.generation = 0;
    this.current = null;
  }
  enqueue(text, playerId, options = {}) {
    return new Promise((resolve) => {
      this.queue.push({ text, playerId, options, resolve });
      this.drain();
    });
  }
  async drain() {
    if (this.running) return;
    this.running = true;
    const generation = this.generation;
    let failed = false;
    try {
      while (this.queue.length && generation === this.generation) {
        const item = this.queue.shift();
        this.current = item;
        this.onSpeaker(item.playerId);
        this.onStatus(`玩家 ${item.playerId} 正在发言`);
        try {
          await this.adapter.speak(item.text, item.options);
          if (generation === this.generation) item.resolve({ status: "finished" });
        } catch (error) {
          if (generation !== this.generation) break;
          failed = true;
          item.resolve({ status: "error", message: error.message });
          for (const queued of this.queue.splice(0)) queued.resolve({ status: "canceled" });
          if (generation === this.generation) this.onStatus(error.message);
          break;
        }
      }
    } finally {
      if (generation === this.generation) {
        this.current = null;
        this.running = false;
        this.onSpeaker(null);
        if (!failed) this.onStatus("发言播放完毕");
      }
    }
  }
  clear() {
    this.generation += 1;
    this.current?.resolve({ status: "canceled" });
    this.current = null;
    for (const item of this.queue.splice(0)) item.resolve({ status: "canceled" });
    this.running = false;
    this.adapter.cancel();
    this.onSpeaker(null);
    this.onStatus("朗读已停止");
  }
}
