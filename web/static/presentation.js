/** 将真实流程快照转换为播放中的展示状态，规则状态始终留在 Python。 */
export function playbackSnapshot(snapshot) {
  if (!snapshot.playback) return snapshot;
  return {
    ...snapshot,
    phase: "day",
    active_player: snapshot.playback.player_id,
    discussion_round: snapshot.playback.discussion_round,
    events: snapshot.events.slice(0, snapshot.playback.event_index + 1),
  };
}

/** 好人夜晚遮屏，轮到本人的夜间技能时只开放行动面板。狼人可查看自己的夜间界面。 */
export function nightCurtain(snapshot) {
  const stage = snapshot.events.filter((event) => ["night_started", "day_started"].includes(event.kind)).at(-1);
  const night = snapshot.phase === "night" || (snapshot.phase === "hunter" && stage?.kind === "night_started");
  const closed = ["ready", "running", "waiting"].includes(snapshot.status) && night && snapshot.private?.role !== "werewolf";
  return { closed, ownAction: closed && Boolean(snapshot.pending) };
}
