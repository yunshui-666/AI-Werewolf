/**
 * 浏览器控制器。
 * 只保存界面偏好与服务端返回的玩家视图，规则判定和隐藏身份始终位于 Python。
 * 所有模型发言使用 textContent 插入，避免把游戏文本当 HTML 执行。
 */
import { VoiceAdapter, SpeechQueue, VOICE_PRESETS, defaultVoice, DEFAULT_SPEECH_RATE } from "/speech.js";
import { playbackSnapshot, nightCurtain } from "/presentation.js";

const $ = (selector) => document.querySelector(selector);
const roleNames = { werewolf: "狼人", seer: "预言家", witch: "女巫", hunter: "猎人", guard: "守卫", villager: "村民" };
const phaseNames = { night: "夜晚行动", day: "白天讨论", voting: "投票阶段", hunter: "技能响应", finished: "终局揭晓" };
const actionNames = { speak: "轮到你公开发言", wolf_discuss: "狼人私下交流", wolf_kill: "选择今晚击杀目标", inspect: "选择查验目标", guard: "选择守护目标", witch: "决定本夜用药", hunter_shoot: "决定是否发动猎人技能", vote: "选择放逐目标" };
const outcomes = { good: "好人阵营获胜", werewolf: "狼人阵营获胜", draw: "本局平局" };
const state = {
  setup: null, sessionId: null, snapshot: null, pendingId: null,
  selectedTarget: null, potion: "pass", filter: "all", eventCount: 0,
  profiles: [], selections: {}, voicePrefs: {}, skills: null, currentSkill: "villager",
  speechRate: DEFAULT_SPEECH_RATE, setupVersion: 0, pollVersion: 0, playbackId: null, completedPlayback: null, confirmingPlayback: false,
  progress: new Map(), nightRequestId: null,
};
let pollTimer = null;
let voiceSpeaker = null;

// localStorage 只保存接口 ID 和声线偏好，实际 Key 从不写入浏览器持久存储。
try {
  state.selections = JSON.parse(localStorage.getItem("werewolf.selections") || "{}");
  // 升级时将旧版默认离线选择迁移到自动路由，已选的自定义接口继续保留。
  if (!localStorage.getItem("werewolf.routing.v2")) {
    Object.keys(state.selections).forEach((id) => {
      if (state.selections[id] === "mock") state.selections[id] = "auto";
    });
    localStorage.setItem("werewolf.selections", JSON.stringify(state.selections));
    localStorage.setItem("werewolf.routing.v2", "1");
  }
  const savedRate = Number(localStorage.getItem("werewolf.speechRate") || DEFAULT_SPEECH_RATE);
  if ([0.8, 1, 1.3, 1.6, 2].includes(savedRate)) state.speechRate = savedRate;
  state.voicePrefs = JSON.parse(localStorage.getItem("werewolf.voices") || "{}");
} catch { /* 存储被禁用或数据损坏时使用本次会话的默认配置。 */ }

const tts = new VoiceAdapter();
const speechQueue = new SpeechQueue(tts,
  (message) => { $("#voice-status").textContent = message; },
  (playerId) => { voiceSpeaker = playerId; updateHighlights(); },
);

// 统一的同源 JSON 请求。非成功状态转换为可展示的固定业务错误，调用方决定如何反馈。
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "本地服务请求失败");
    error.status = response.status;
    throw error;
  }
  return data;
}

// 显示短时提示，重复提示会更新内容和计时，避免叠加多层消息。
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("visible");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => $("#toast").classList.remove("visible"), 3500);
}

// 仅持久化非敏感界面偏好。接口 Key 留在 Python 配置或当前服务内存。
function savePreferences() {
  try {
    localStorage.setItem("werewolf.selections", JSON.stringify(state.selections));
    localStorage.setItem("werewolf.voices", JSON.stringify(state.voicePrefs));
    localStorage.setItem("werewolf.speechRate", String(state.speechRate));
  } catch { /* 偏好保存失败不影响当前对局。 */ }
}

// 根据当前板子决定展示座位数，真正人数仍由后端验证。
function seatCount() {
  return $("#preset").value === "standard12" ? 12 : 8;
}

// 开局前生成中性座位展示，所有角色字段为空，避免头像与隐藏身份发生关联。
function initialPlayers() {
  return Array.from({ length: seatCount() }, (_, index) => ({
    player_id: index + 1, alive: true, role: null,
    human: index + 1 === Number($("#human-seat").value),
  }));
}

// 切换板子时更新座位、角色数量和逐 Agent 配置，保留仍合法的真人座位。
function renderSetup() {
  const previous = Number($("#human-seat").value || 1);
  $("#human-seat").replaceChildren();
  for (let id = 0; id <= seatCount(); id += 1) {
    $("#human-seat").add(new Option(id ? `玩家 ${id}（你）` : "全 AI · 观战", String(id)));
  }
  $("#human-seat").value = String(previous <= seatCount() ? previous : 1);
  const preset = state.setup.presets[$("#preset").value];
  $("#role-counts").textContent = Object.entries(preset).map(([role, count]) => `${roleNames[role]} × ${count}`).join(" · ");
  if (!state.snapshot) renderPlayers(initialPlayers());
  renderSeatConfigs();
}

/** 座位位置仅取决于编号。未知身份头像也按编号着色，与真实底牌没有关联。 */
function renderPlayers(players) {
  // 保留未变化的座位 DOM，避免轮询打断鼠标点击或键盘焦点。
  const signature = JSON.stringify(players) + (window.innerWidth <= 720 ? "mobile" : "desktop");
  if (signature === renderPlayers.lastSignature) {
    updateHighlights();
    return;
  }
  renderPlayers.lastSignature = signature;
  const root = $("#players");
  root.replaceChildren();
  const radiusX = window.innerWidth <= 720 ? 37 : 42;
  players.forEach((player, index) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / players.length;
    const card = document.createElement("div");
    card.title = player.profile_name || "";
    card.className = `player${player.alive ? "" : " dead"}${player.human ? " human" : ""}`;
    card.dataset.id = player.player_id;
    card.style.setProperty("--x", `${50 + radiusX * Math.cos(angle)}%`);
    card.style.setProperty("--y", `${50 + 38 * Math.sin(angle)}%`);
    const button = document.createElement("button");
    button.className = "player-button";
    button.setAttribute("aria-label", `选择玩家 ${player.player_id}${player.alive ? "" : "，已出局"}`);
    const avatar = document.createElement("div");
    avatar.className = "player-avatar portrait";
    avatar.dataset.role = player.role || "villager";
    if (!player.role) avatar.style.filter = `saturate(${0.65 + (index % 3) * 0.15})`;
    const number = document.createElement("span");
    number.className = "player-number";
    number.textContent = player.player_id;
    avatar.append(number);
    const name = document.createElement("div");
    name.className = "player-name";
    name.textContent = `玩家 ${player.player_id}${player.human ? " · 你" : ""}`;
    const role = document.createElement("div");
    role.className = "player-role";
    role.textContent = !player.alive ? "已出局" : player.role ? roleNames[player.role] : "身份未公开";
    button.append(avatar, name, role);
    button.addEventListener("click", () => selectPlayer(player.player_id));
    card.append(button);
    root.append(card);
  });
  updateHighlights();
}

// 分别标记当前公开发言、语音播放和用户所选目标，三个状态彼此独立。
function updateHighlights() {
  const pending = state.snapshot?.pending;
  document.querySelectorAll(".player").forEach((card) => {
    const id = Number(card.dataset.id);
    card.classList.toggle("active", (voiceSpeaker || state.snapshot?.active_player) === id);
    card.classList.toggle("voice-playing", voiceSpeaker === id);
    card.classList.toggle("selected", state.selectedTarget === id);
    card.classList.toggle("selectable", Boolean(pending?.candidates.includes(id)));
  });
  document.querySelectorAll(".target-buttons button").forEach((button) => {
    button.classList.toggle("selected", Number(button.dataset.target) === state.selectedTarget);
  });
}

// 圆桌点击只负责输入辅助。发言插入玩家编号，技能选择仍受本次合法候选限制。
function selectPlayer(id) {
  const pending = state.snapshot?.pending;
  if (!pending) return;
  if (["speak", "wolf_discuss"].includes(pending.action)) {
    const input = $("#speech-input");
    if (input) { input.value += `玩家 ${id} `; input.focus(); }
    return;
  }
  if (!pending.candidates.includes(id)) return toast("请从本次合法候选中选择目标");
  if (pending.action === "witch" && state.potion === "pass") return toast("请先选择解药或毒药");
  if (pending.action === "witch" && state.potion === "save" && id !== state.snapshot.private.night_victim) {
    return toast("解药只能救本夜刀口");
  }
  state.selectedTarget = id;
  updateHighlights();
}

// 只读取后端返回的本人 PlayerView，把身份、已加载 skill 和私有技能结果放入左侧面板。
function renderPrivate(snapshot) {
  const view = snapshot.private;
  $("#private-info").replaceChildren();
  if (!view) {
    $(".own-portrait").dataset.role = "villager";
    $("#own-role").textContent = "观战模式";
    $("#own-description").textContent = "全部座位由 AI 控制，终局后统一亮牌。";
    $("#own-skill").disabled = true;
    return;
  }
  $(".own-portrait").dataset.role = view.role;
  $("#own-role").textContent = roleNames[view.role];
  $("#own-description").textContent = `玩家 ${view.player_id} · ${view.personality} · ${view.alive ? "存活" : "已出局"}`;
  $("#own-skill").disabled = !view.role_skill;
  $("#own-skill").textContent = "查看已加载 skill";
  const lines = [];
  if (view.role_skill) lines.push(`已加载 ${view.role_skill.name}`);
  if (view.wolf_teammates.length) lines.push(`狼人队友为 ${view.wolf_teammates.map((id) => `玩家 ${id}`).join("、")}`);
  if (view.role === "witch") {
    lines.push(`解药 ${Number(view.antidote_available)} 瓶 · 毒药 ${Number(view.poison_available)} 瓶`);
    if (view.night_victim) lines.push(`本夜刀口为玩家 ${view.night_victim}`);
  }
  for (const result of view.inspections) {
    lines.push(`第 ${result.round_number} 夜，玩家 ${result.target_id} 为${result.team === "werewolf" ? "狼人" : "好人"}`);
  }
  if (view.role === "guard" && view.previous_guard_target) lines.push(`上次守护玩家 ${view.previous_guard_target}`);
  if (view.role === "werewolf") lines.push(...view.private_notes.slice(-3));
  lines.forEach((line) => {
    const item = document.createElement("p");
    item.textContent = line;
    $("#private-info").append(item);
  });
}

/** 每次新输入请求才重建表单，轮询期间保留用户正在编辑的发言。 */
function renderAction(snapshot) {
  const pending = snapshot.pending;
  if (pending?.request_id === state.pendingId) return;
  state.pendingId = pending?.request_id || null;
  state.selectedTarget = null;
  state.potion = "pass";
  $("#action-content").replaceChildren();
  $("#action-error").textContent = "";
  if (!pending) {
    $("#action-title").textContent = snapshot.outcome ? outcomes[snapshot.outcome] : snapshot.status === "stopped" ? "本局已停止" : "等待其他玩家行动";
    $("#action-hint").textContent = snapshot.outcome ? `降级行动 ${snapshot.fallback_count} 次` : "轮到你时会出现输入面板";
    const note = document.createElement("p");
    note.className = snapshot.outcome ? "outcome" : "muted small";
    note.textContent = snapshot.outcome ? "全部身份已揭晓，可查看圆桌记录或重新开局。" : snapshot.error || (snapshot.status === "stopped" ? "可调整配置后重新开始一局。" : snapshot.private && !snapshot.private.alive ? "你已出局，继续观看公开过程。" : "AI 正根据各自的信息进行决策。");
    $("#action-content").append(note);
    updateHighlights();
    return;
  }
  $("#action-title").textContent = actionNames[pending.action];
  $("#action-hint").textContent = ["wolf_discuss", "wolf_kill", "inspect", "guard", "witch"].includes(pending.action) ? "这是你的私有行动" : "请确认后提交";
  if (["speak", "wolf_discuss"].includes(pending.action)) {
    const input = document.createElement("textarea");
    input.id = "speech-input";
    input.maxLength = 1200;
    input.placeholder = pending.action === "speak" ? "说出你的证据、问题和投票意向……" : "与狼人队友讨论今晚的选择……";
    input.setAttribute("aria-label", actionNames[pending.action]);
    $("#action-content").append(input);
  } else {
    if (pending.action === "witch") renderPotionButtons(snapshot);
    const targets = document.createElement("div");
    targets.className = "target-buttons";
    if (["guard", "hunter_shoot"].includes(pending.action)) {
      const skip = document.createElement("button");
      skip.textContent = pending.action === "guard" ? "空守" : "不开枪";
      skip.addEventListener("click", () => { state.selectedTarget = null; updateHighlights(); });
      targets.append(skip);
    }
    pending.candidates.forEach((id) => {
      const button = document.createElement("button");
      button.textContent = `玩家 ${id}`;
      button.dataset.target = id;
      button.addEventListener("click", () => selectPlayer(id));
      targets.append(button);
    });
    $("#action-content").append(targets);
  }
  const bottom = document.createElement("div");
  bottom.className = "action-bottom";
  const note = document.createElement("span");
  note.className = "muted small";
  note.textContent = "也可点击圆桌上的玩家选择目标";
  const submit = document.createElement("button");
  submit.id = "submit-action";
  submit.className = "primary";
  submit.textContent = "确认提交";
  submit.addEventListener("click", submitAction);
  bottom.append(note, submit);
  $("#action-content").append(bottom);
  updateHighlights();
}

// 根据本人剩余药品和刀口启用按钮，解药自动选中刀口，真正资源消耗由后端执行。
function renderPotionButtons(snapshot) {
  const root = document.createElement("div");
  root.className = "potion-buttons";
  const view = snapshot.private;
  for (const [potion, label] of [["pass", "保留药品"], ["save", "使用解药"], ["poison", "使用毒药"]]) {
    const button = document.createElement("button");
    button.textContent = label;
    button.classList.toggle("selected", potion === "pass");
    button.disabled = potion === "save" ? !view.antidote_available || !view.night_victim || view.night_victim === view.player_id : potion === "poison" && !view.poison_available;
    button.addEventListener("click", () => {
      state.potion = potion;
      state.selectedTarget = potion === "save" ? view.night_victim : null;
      root.querySelectorAll("button").forEach((item) => item.classList.toggle("selected", item === button));
      updateHighlights();
    });
    root.append(button);
  }
  $("#action-content").append(root);
}

// 携带当前 request_id 提交一次建议。成功后轮询新状态，失败时保留用户输入以便纠正。
async function submitAction() {
  const pending = state.snapshot?.pending;
  if (!pending) return;
  const decision = { target_id: state.selectedTarget, text: "", suspicions: [], potion: "pass" };
  if (["speak", "wolf_discuss"].includes(pending.action)) {
    decision.text = $("#speech-input").value.trim();
    if (!decision.text) return toast("请先输入发言");
  } else if (pending.action === "witch") {
    decision.potion = state.potion;
    if (state.potion === "pass") decision.target_id = null;
  }
  if (["vote", "inspect", "wolf_kill"].includes(pending.action) && decision.target_id === null) return toast("请选择目标玩家");
  const submit = $("#submit-action");
  submit.disabled = true;
  try {
    await api("/api/game/action", { session_id: state.sessionId, request_id: pending.request_id, decision });
    toast("行动已提交");
    await poll();
  } catch (error) {
    $("#action-error").textContent = error.message;
    submit.disabled = false;
  }
}

// 按筛选条件展示公开历史，模型文本始终作为文本节点写入，并保留用户正在阅读的滚动位置。
function renderEvents(snapshot) {
  const root = $("#events");
  const nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 100;
  const previousTop = root.scrollTop;
  const changed = snapshot.events.length !== state.eventCount;
  if (!changed && state.filter === renderEvents.lastFilter) return;
  root.replaceChildren();
  snapshot.events.forEach((event, index) => {
    if (state.filter === "speech" && event.kind !== "speech") return;
    if (state.filter === "vote" && !["vote_result", "vote_tie", "exile"].includes(event.kind)) return;
    const node = document.createElement("div");
    node.dataset.eventIndex = index;
    const chapter = ["night_started", "day_started", "discussion_round_started", "game_finished"].includes(event.kind);
    node.className = `event ${event.kind === "speech" ? "speech" : chapter ? "chapter" : "system"}`;
    if (event.kind === "speech") {
      const heading = document.createElement("div");
      heading.className = "speaker";
      const name = document.createElement("span");
      const sourceNames = { api: "API", mock: "离线", fallback: "API 失败 · 离线降级", human: "你", agent: "自定义 Agent" };
      const player = snapshot.players.find((item) => item.player_id === event.actor_id);
      const source = sourceNames[event.decision_source];
      name.textContent = `玩家 ${event.actor_id} · 第 ${event.round_number} 天${source ? ` · ${source}` : ""}`;
      name.title = player?.profile_name || "";
      if (event.decision_source === "fallback") heading.classList.add("fallback-source");
      const replay = document.createElement("button");
      replay.textContent = "朗读";
      replay.addEventListener("click", () => speakEvent(event));
      heading.append(name, replay);
      const text = document.createElement("div");
      text.className = "speech-text";
      const body = event.text.replace(/^玩家 \d+：/, "");
      const progress = state.progress.get(Number(node.dataset.eventIndex));
      text.textContent = progress === undefined ? body : body.slice(0, progress) || "正在准备发言…";
      text.setAttribute("aria-live", "off");
      node.append(heading, text);
    } else {
      node.textContent = event.text.replace(/=+/g, "").trim();
    }
    root.append(node);
  });
  state.eventCount = snapshot.events.length;
  renderEvents.lastFilter = state.filter;
  if (nearBottom) root.scrollTop = root.scrollHeight;
  else root.scrollTop = previousTop;
}

// 字幕只更新当前文本节点，轮询和筛选重绘时从 progress 恢复，避免突然显示整段话。
function updateSpeechText(eventIndex, count) {
  const event = state.snapshot?.events[eventIndex];
  if (!event) return;
  const text = event.text.replace(/^玩家 \d+：/, "");
  const progress = Math.min(text.length, Math.max(state.progress.get(eventIndex) || 0, count));
  state.progress.set(eventIndex, progress);
  const root = $("#events");
  const atBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 110;
  const node = root.querySelector(`[data-event-index="${eventIndex}"] .speech-text`);
  if (node) node.textContent = text.slice(0, progress) || "正在准备发言…";
  if (atBottom) root.scrollTop = root.scrollHeight;
}

function voiceOptions(playerId, eventIndex) {
  return {
    preset: state.voicePrefs[playerId]?.preset || defaultVoice(playerId),
    sessionId: state.sessionId, eventIndex, rate: state.speechRate,
  };
}

// 历史朗读沿用串行队列，当前 AI 尚未播完时优先完成本轮发言。
function speakEvent(event) {
  if (state.snapshot?.playback) return toast("请先听完当前玩家，随后可以重播历史发言");
  const index = state.snapshot.events.indexOf(event);
  tts.unlock(voiceOptions(event.actor_id, index).preset);
  speechQueue.enqueue(event.text.replace(/^玩家 \d+：/, ""), event.actor_id, voiceOptions(event.actor_id, index));
}

async function confirmPlayback(playback) {
  if (state.confirmingPlayback) return;
  const session = state.sessionId;
  state.confirmingPlayback = true;
  try {
    await api("/api/game/playback", { session_id: session, playback_id: playback.playback_id });
    if (state.sessionId === session) {
      $("#retry-speech").hidden = true;
      await poll();
    }
  } catch (error) {
    $("#voice-status").textContent = error.message;
    $("#retry-speech").textContent = "继续游戏";
    $("#retry-speech").hidden = false;
  } finally { state.confirmingPlayback = false; }
}

// 后端只提供一个待播放令牌。实际音频结束后才确认，下一位才会被主持人调用。
function playPending(snapshot, offset = 0) {
  const playback = snapshot.playback;
  if (!playback || state.playbackId === playback.playback_id) return;
  state.playbackId = playback.playback_id;
  const session = state.sessionId;
  const event = snapshot.events[playback.event_index];
  const text = event.text.replace(/^玩家 \d+：/, "");
  $("#retry-speech").hidden = true;
  speechQueue.enqueue(text.slice(offset), event.actor_id, {
    ...voiceOptions(event.actor_id, playback.event_index),
    silent: !$("#speech-enabled").checked,
    onProgress: (count) => {
      if (state.sessionId === session && state.playbackId === playback.playback_id) {
        updateSpeechText(playback.event_index, offset + count);
      }
    },
  }).then((result) => {
    if (result.status === "canceled" || state.sessionId !== session || state.playbackId !== playback.playback_id) return;
    if (result.status === "error") {
      $("#retry-speech").textContent = "重试语音";
      $("#retry-speech").hidden = false;
      return;
    }
    updateSpeechText(playback.event_index, text.length);
    state.completedPlayback = playback.playback_id;
    confirmPlayback(playback);
  });
}

function renderNight(snapshot) {
  const mode = nightCurtain(snapshot);
  const wasClosed = document.body.classList.contains("night-closed");
  document.body.classList.toggle("night-closed", mode.closed);
  document.body.classList.toggle("night-own-action", mode.ownAction);
  $("#night-curtain").hidden = !mode.closed;
  $("#night-message").textContent = mode.ownAction ? "轮到你使用夜间技能" : "等待天亮…";
  for (const selector of [".topbar", ".sidebar", ".conversation", "#arena", ".stage-header"]) {
    $(selector).inert = mode.closed;
  }
  $(".action-panel").inert = mode.closed && !mode.ownAction;
  const tableHint = $(".action-bottom .muted");
  if (tableHint) tableHint.hidden = mode.ownAction;
  if (mode.closed) document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  if (mode.ownAction && state.nightRequestId !== snapshot.pending.request_id) {
    state.nightRequestId = snapshot.pending.request_id;
    $(".action-panel").querySelector("textarea, input, button:not(:disabled)")?.focus({ preventScroll: true });
  } else if (mode.closed && !wasClosed) $("#night-heading").focus({ preventScroll: true });
  if (!mode.ownAction) state.nightRequestId = null;
}

// 把一份白名单快照分发到圆桌、本人面板、行动表单和记录，结束后才展示全部身份。
function renderSnapshot(snapshot) {
  snapshot = playbackSnapshot(snapshot);
  if (snapshot.playback && !state.progress.has(snapshot.playback.event_index)) {
    state.progress.set(snapshot.playback.event_index, 0);
  }
  state.snapshot = snapshot;
  if ($("#preset").value !== snapshot.preset) {
    $("#preset").value = snapshot.preset;
    renderSetup();
  }
  $("#human-seat").value = String(snapshot.private?.player_id || 0);
  const title = snapshot.status === "stopped" ? "对局已停止" : snapshot.outcome ? outcomes[snapshot.outcome] : `第 ${snapshot.round_number} ${snapshot.phase === "night" ? "夜" : "天"}`;
  $("#phase-title").textContent = title;
  $("#phase-detail").textContent = `${phaseNames[snapshot.phase]}${snapshot.phase === "day" ? ` · 第 ${snapshot.discussion_round}/${snapshot.discussion_rounds} 轮发言` : ""}`;
  $("#arena").dataset.phase = snapshot.phase;
  $("#table-status").textContent = snapshot.status === "stopped" ? "等待重新入席" : snapshot.outcome ? "终局亮牌" : phaseNames[snapshot.phase];
  $("#table-subtitle").textContent = snapshot.pending ? "轮到你作出决定" : snapshot.active_player ? `玩家 ${snapshot.active_player} 正在发言` : "保持观察，等待线索";
  $("#alive-count").textContent = `${snapshot.players.filter((player) => player.alive).length} / ${snapshot.players.length} 位存活`;
  const running = ["running", "waiting", "stopping", "ready"].includes(snapshot.status);
  $("#start").disabled = running;
  $("#stop").disabled = !running || snapshot.status === "stopping";
  $("#preset").disabled = running;
  $("#human-seat").disabled = running;
  renderPlayers(snapshot.players);
  renderPrivate(snapshot);
  renderAction(snapshot);
  renderEvents(snapshot);
  renderNight(snapshot);
  playPending(snapshot);
  if (snapshot.error) toast(snapshot.error);
}

// 每次最多应用最新轮询结果，忽略旧会话或乱序响应，防止已提交表单被过期快照覆盖。
async function poll() {
  window.clearTimeout(pollTimer);
  if (!state.sessionId) return;
  const requestedSession = state.sessionId;
  const version = ++state.pollVersion;
  try {
    const snapshot = await api(`/api/game?session=${encodeURIComponent(requestedSession)}`);
    if (version !== state.pollVersion || requestedSession !== state.sessionId) return;
    renderSnapshot(snapshot);
    if (["running", "waiting", "stopping", "ready"].includes(snapshot.status)) pollTimer = window.setTimeout(poll, 250);
  } catch (error) {
    if (version !== state.pollVersion) return;
    if (error.status === 404) {
      speechQueue.clear();
      renderNight({ status: "stopped", phase: "finished", events: [], private: null, pending: null });
      state.snapshot = null;
      state.pendingId = null;
      state.playbackId = null;
      state.sessionId = null;
      sessionStorage.removeItem("werewolf.current");
      $("#start").disabled = false;
      $("#stop").disabled = true;
      $("#preset").disabled = false;
      $("#human-seat").disabled = false;
      toast("上次服务中的对局已结束，可以重新开局。");
      return;
    }
    toast(error.message);
    pollTimer = window.setTimeout(poll, 1600);
  }
}

// 收集开局参数和逐座位接口 ID，服务端返回新的随机会话 ID 后才开始状态轮询。
async function startGame() {
  tts.unlock(Object.values(state.voicePrefs).some((value) => value.preset === "doubao") ? "doubao" : "gentle_female");
  $("#start").disabled = true;
  window.clearTimeout(pollTimer);
  state.pollVersion += 1;
  speechQueue.clear();
  try {
    const snapshot = await api("/api/game/start", {
      preset: $("#preset").value, human_id: Number($("#human-seat").value),
      seed: $("#seed").value === "" ? null : Number($("#seed").value),
      discussion_rounds: Number($("#rounds").value), delay_ms: Number($("#delay").value),
      playback_sync: true,
      selections: Object.fromEntries(Object.entries(state.selections).filter(([id]) => Number(id) <= seatCount())),
    });
    state.sessionId = snapshot.session_id;
    sessionStorage.setItem("werewolf.current", state.sessionId);
    state.snapshot = null;
    state.pendingId = null;
    state.eventCount = 0;
    state.playbackId = null;
    state.completedPlayback = null;
    state.progress = new Map();
    renderSnapshot(snapshot);
    pollTimer = window.setTimeout(poll, 500);
  } catch (error) {
    toast(error.message);
    $("#start").disabled = false;
  }
}

// 每个座位选择 API 与三种产品声线，底层系统声音选择交给语音适配器。
function renderSeatConfigs() {
  const root = $("#seat-configs");
  root.replaceChildren();
  for (let id = 1; id <= seatCount(); id += 1) {
    const row = document.createElement("div");
    row.className = "seat-config-row";
    const label = document.createElement("strong");
    const human = id === Number($("#human-seat").value);
    label.textContent = `玩家 ${id}${human ? " · 你" : ""}`;
    const profile = document.createElement("select");
    profile.setAttribute("aria-label", `玩家 ${id} 的 API`);
    const defaultId = state.setup?.seat_defaults?.[id];
    const defaultProfile = state.profiles.find((item) => item.profile_id === defaultId);
    profile.add(new Option(human ? "真人输入" : `跟随 .env · ${defaultProfile?.name || "配置待检查"}`, "auto"));
    state.profiles.forEach((item) => profile.add(new Option(`${item.name}${item.has_key ? "" : " · 待填 Key"}`, item.profile_id)));
    profile.value = state.selections[id] || "auto";
    profile.disabled = human;
    profile.addEventListener("change", async () => {
      state.selections[id] = profile.value;
      savePreferences();
      try { await refreshProfiles(); renderSeatConfigs(); }
      catch (error) { toast(error.message); }
    });
    const voice = document.createElement("select");
    voice.setAttribute("aria-label", `玩家 ${id} 的声线`);
    VOICE_PRESETS.forEach((item) => {
      const ready = item.id !== "doubao" || state.setup.voice.doubao_ready;
      const option = new Option(`${item.name}${ready ? "" : " · 待配置"}`, item.id);
      option.disabled = !ready;
      voice.add(option);
    });
    voice.value = state.voicePrefs[id]?.preset || defaultVoice(id);
    voice.addEventListener("change", () => { state.voicePrefs[id] = { preset: voice.value }; savePreferences(); });
    row.append(label, profile, voice);
    root.append(row);
  }
}

/** 一个小型安全 Markdown 展示器，先转义文本，再转换标题与 HTTPS 链接。 */
function displayMarkdown(root, markdown) {
  root.replaceChildren();
  for (const line of markdown.split("\n")) {
    const block = document.createElement(line.startsWith("#") ? "h2" : "p");
    const text = line.replace(/^#+\s*/, "");
    const pattern = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      block.append(document.createTextNode(text.slice(cursor, match.index)));
      const link = document.createElement("a");
      link.href = match[2]; link.textContent = match[1]; link.target = "_blank"; link.rel = "noopener noreferrer";
      block.append(link);
      cursor = match.index + match[0].length;
    }
    block.append(document.createTextNode(text.slice(cursor)));
    root.append(block);
  }
}

// 从本地后端读取六份通用角色攻略，选中本人 skill 时只影响说明面板，不改变游戏身份。
async function showSkills(role = "villager") {
  if (!state.skills) {
    state.skills = await api("/api/skills");
    const research = await api("/api/research");
    displayMarkdown($("#research-content"), research.markdown);
  }
  state.currentSkill = role;
  $("#skill-tabs").replaceChildren();
  Object.keys(roleNames).forEach((name) => {
    const button = document.createElement("button");
    button.textContent = roleNames[name];
    button.classList.toggle("selected", name === role);
    button.addEventListener("click", () => showSkills(name));
    $("#skill-tabs").append(button);
  });
  displayMarkdown($("#skill-content"), state.skills[role].instructions);
  if (!$("#guide-dialog").open) $("#guide-dialog").showModal();
}

// 事件绑定集中在启动入口，避免每次轮询重复注册全局事件。
$("#preset").addEventListener("change", async () => {
  renderSetup();
  try { await refreshProfiles(); renderSeatConfigs(); }
  catch (error) { toast(error.message); }
});
$("#human-seat").addEventListener("change", async () => {
  if (!state.snapshot) renderPlayers(initialPlayers());
  try { await refreshProfiles(); renderSeatConfigs(); }
  catch (error) { toast(error.message); }
});
$("#speech-rate").value = String(state.speechRate);
$("#speech-rate").addEventListener("change", () => {
  state.speechRate = Number($("#speech-rate").value);
  savePreferences();
  toast(`后续发言使用 ${state.speechRate} 倍语速`);
});
$("#delay").addEventListener("input", () => { $("#delay-label").textContent = `${(Number($("#delay").value) / 1000).toFixed(1)} 秒`; });
$("#start").addEventListener("click", startGame);
async function stopGame() {
  try {
    const playback = state.snapshot?.playback;
    if (playback) updateSpeechText(playback.event_index, state.snapshot.events[playback.event_index].text.length);
    speechQueue.clear();
    await api("/api/game/stop", { session_id: state.sessionId });
    await poll();
  } catch (error) { toast(error.message); }
}
$("#stop").addEventListener("click", stopGame);
$("#night-stop").addEventListener("click", stopGame);
$("#speech-enabled").addEventListener("change", () => {
  const playback = state.snapshot?.playback;
  if (!$("#speech-enabled").checked && playback && state.completedPlayback !== playback.playback_id) {
    const offset = state.progress.get(playback.event_index) || 0;
    speechQueue.clear();
    state.playbackId = null;
    playPending(state.snapshot, offset);
  } else if (!$("#speech-enabled").checked) speechQueue.clear();
  $("#voice-status").textContent = $("#speech-enabled").checked ? "下一条发言启用语音" : "逐字文字模式";
});
$("#clear-speech").addEventListener("click", () => {
  speechQueue.clear();
  const playback = state.snapshot?.playback;
  if (playback) {
    updateSpeechText(playback.event_index, state.snapshot.events[playback.event_index].text.length);
    state.completedPlayback = playback.playback_id;
    confirmPlayback(playback);
  }
});
$("#retry-speech").addEventListener("click", () => {
  const playback = state.snapshot?.playback;
  if (!playback) return;
  if (state.completedPlayback === playback.playback_id) return confirmPlayback(playback);
  tts.unlock(voiceOptions(playback.player_id, playback.event_index).preset);
  state.progress.set(playback.event_index, 0);
  state.playbackId = null;
  playPending(state.snapshot);
});
// 打开设置时重新读取 .env，新补填的模型无需重启服务。
async function refreshProfiles() {
  const version = ++state.setupVersion;
  const query = new URLSearchParams({
    preset: $("#preset").value,
    human_id: $("#human-seat").value || "1",
    selections: JSON.stringify(Object.fromEntries(Object.entries(state.selections).filter(([id]) => Number(id) <= seatCount()))),
  });
  const setup = await api(`/api/setup?${query}`);
  if (version !== state.setupVersion) return;
  state.setup = setup;
  state.profiles = state.setup.profiles;
  const defaults = Object.values(state.setup.seat_defaults);
  const remoteCount = defaults.filter((id) => id && id !== "mock").length;
  const poolCount = state.setup.deepseek_pool?.length || 0;
  $("#provider-summary").textContent = remoteCount
    ? `${poolCount > 1 ? `已识别 ${poolCount} 组 DeepSeek 配置，可自动循环分配。` : ""}已为 ${remoteCount} 个座位选择 API，下一局生效。`
    : "环境配置当前使用离线策略，填写 API Key、Base URL 和模型后可自动接入。";
  $("#provider-warnings").textContent = state.setup.config_warnings.join("；");
}
$("#profiles-button").addEventListener("click", async () => {
  try { await refreshProfiles(); renderSeatConfigs(); $("#profiles-dialog").showModal(); }
  catch (error) { toast(error.message); }
});
$("#use-env").addEventListener("click", async () => {
  try {
    state.selections = {};
    savePreferences();
    await refreshProfiles();
    renderSeatConfigs();
    toast("所有 AI 已设为跟随 .env，下一局生效。");
  } catch (error) { toast(error.message); }
});
$("#guide-button").addEventListener("click", () => showSkills().catch((error) => toast(error.message)));
$("#own-skill").addEventListener("click", () => showSkills(state.snapshot.private.role).catch((error) => toast(error.message)));
$("#flow-button").addEventListener("click", () => $("#flow-dialog").showModal());
document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
  state.filter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("selected", item === button));
  if (state.snapshot) renderEvents(state.snapshot);
}));
$("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  const apiKey = values.api_key;
  delete values.api_key;
  values.requires_key = form.elements.requires_key.checked;
  try {
    await api("/api/profiles", { profile: values, api_key: apiKey });
    await refreshProfiles();
    form.elements.api_key.value = "";
    $("#profile-message").textContent = "配置已保存。Key 仅在当前服务运行期间保存在后端内存中。";
    renderSeatConfigs();
  } catch (error) { $("#profile-message").textContent = error.message; }
});
window.addEventListener("resize", () => {
  if (state.setup) renderPlayers(state.snapshot?.players || initialPlayers());
});

try {
  await refreshProfiles();
  renderSetup();
  // 同一标签页刷新后恢复本人的会话，避免后端仍在等待而前端失去提交入口。
  const savedSession = sessionStorage.getItem("werewolf.current");
  if (savedSession) {
    state.sessionId = savedSession;
    await poll();
  }
} catch (error) {
  toast("无法连接本地后端，请通过 python play_web.py 启动。");
  $("#start").disabled = true;
}
