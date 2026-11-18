"""从个人可见信息提取博弈信号。分数是可调启发式，正分表示更可疑。"""

from dataclasses import dataclass
import re

from agents.memory import Team
from game.state import PlayerView, Role

SEER_CLAIM = re.compile(r"(?:我是|我才是|我就是|我跳|我也跳|我起跳|身份是)\s*(?:真正的)?预言家")
CHECK_CLAIM = re.compile(
    r"(?:查验(?:了|过)?|验了|验过)\s*(?:玩家\s*)?(1[0-2]|[1-9])(?!\d)\s*(?:号(?:玩家)?)?"
    r"\s*(?:结果(?:是|为)?|是|为|属于)?\s*(狼人|狼|好人|金水)"
)
VOTE_INTENT = re.compile(
    r"(?:我(?:现在|目前|这轮|本轮|今天)?(?:准备投|决定投|建议投|会投|要投|投给|投)|"
    r"建议(?:大家)?(?:今天)?投|今天投|本轮投)\s*(?:玩家\s*)?(1[0-2]|[1-9])(?!\d)(?:\s*号)?"
)


@dataclass(frozen=True)
class StrategyWeights:
    """可调启发式证据权重。修改这些值可以实验矛盾、票型和公开压力对目标排序的影响。"""
    accusation: float = 2.6
    defense: float = 1.4
    contradiction: float = 5.0
    vote_deviation: float = 1.2
    counterclaim: float = 1.0
    public_pressure: float = 0.35


@dataclass(frozen=True)
class CheckClaim:
    """从公开发言提取的查验声称。它是待检验的公开信号，真实查验仅来自本人私有结果。"""
    speaker_id: int
    target_id: int
    team: Team
    round_number: int


@dataclass(frozen=True)
class Assessment:
    """某座位的启发式怀疑分数及其可追溯依据，正分越高表示越值得质疑。"""
    player_id: int
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class StrategyAnalysis:
    """仅从当前玩家视图推导的分析结果，可同时供离线策略和 LLM Prompt 使用。"""
    assessments: tuple[Assessment, ...]
    seer_claimants: tuple[int, ...]
    checks: tuple[CheckClaim, ...]
    vote_intents: tuple[tuple[int, int], ...]

    def score(self, player_id: int) -> float:
        """返回某座位分数，未知座位使用中性值，便于在合法候选集合中排序。"""
        return next((item.score for item in self.assessments if item.player_id == player_id), 0.0)

    def reason(self, player_id: int) -> str:
        """优先返回能解释当前排序的证据。私有查验事实优先，避免被最后追加的跟票信号掩盖。"""
        assessment = next((item for item in self.assessments if item.player_id == player_id), None)
        if assessment and assessment.reasons:
            if abs(assessment.score) == 100:
                return assessment.reasons[-1]
            return assessment.reasons[0]
        return "目前可核实的信息有限，希望这名玩家说明站边和投票依据。"


def analyze_view(view: PlayerView, weights: StrategyWeights | None = None) -> StrategyAnalysis:
    """按时间扫描公开声明与已结算票型，再结合本人的私有事实评分。重复声明不会无限累积为新证据。"""
    weights = weights or StrategyWeights()
    alive = {player.player_id for player in view.players if player.alive}
    scores = {pid: 0.0 for pid in alive if pid != view.player_id}
    reasons: dict[int, list[str]] = {pid: [] for pid in scores}
    seers: set[int] = set()
    checks: list[CheckClaim] = []
    previous_checks: dict[tuple[int, int], Team] = {}
    contradictions: set[int] = set()
    intents: dict[int, tuple[int, int]] = {}
    deviations: list[tuple[int, str]] = []
    living_at_event = {player.player_id for player in view.players}

    def add(pid: int, amount: float, reason: str) -> None:
        """仅为当前分析中允许出现的其他存活玩家累积分数，并对重复理由去重。"""
        if pid in scores:
            scores[pid] += amount
            if reason not in reasons[pid]:
                reasons[pid].append(reason)

    for event in view.public_events:
        if event.kind == "speech" and event.actor_id is not None:
            speaker = event.actor_id
            if SEER_CLAIM.search(event.text):
                seers.add(speaker)
            for match in CHECK_CLAIM.finditer(event.text):
                target = int(match[1])
                team = Team.WEREWOLF if match[2] in {"狼人", "狼"} else Team.GOOD
                key = (speaker, target)
                if key in previous_checks and previous_checks[key] is not team:
                    contradictions.add(speaker)
                previous_checks[key] = team
                checks.append(CheckClaim(speaker, target, team, event.round_number))
                seers.add(speaker)
            matches = list(VOTE_INTENT.finditer(event.text))
            if matches:
                intents[speaker] = (int(matches[-1][1]), event.round_number)
        elif event.kind == "vote_result":
            for voter, target in event.votes:
                intention = intents.get(voter)
                if intention and intention[1] == event.round_number and intention[0] != target:
                    deviations.append((voter, "公开承诺的投票目标与实际选票不同，需要解释改票原因。"))
                reds = {
                    claim.target_id for claim in checks
                    if claim.speaker_id == voter and claim.team is Team.WEREWOLF
                    and claim.target_id in living_at_event
                }
                if reds and target not in reds:
                    deviations.append((voter, "曾报出仍存活的查杀对象，实际却把票投向其他玩家。"))
        elif event.kind in {"night_death", "exile"}:
            living_at_event.discard(event.target_id)

    known = {result.target_id: result.team for result in view.inspections}
    known[view.player_id] = view.role.team
    if view.role is Role.WEREWOLF:
        known.update({pid: Team.WEREWOLF for pid in view.wolf_teammates})
    for claim in checks:
        if claim.target_id in known and known[claim.target_id] is not claim.team:
            contradictions.add(claim.speaker_id)
    for speaker in contradictions:
        add(speaker, weights.contradiction, "其查验声明前后矛盾，或与我掌握的私有事实冲突。")
    if len(seers) > 1:
        for speaker in seers:
            add(speaker, weights.counterclaim, "存在多个预言家声明，需要比较查验记录和票型。")
    if view.role is Role.SEER:
        for speaker in seers - {view.player_id}:
            add(speaker, weights.contradiction, "我本人是预言家，需要质疑另一名自称预言家的玩家。")

    # 同一人的同一查验重复发言只计一次，避免重复声明累积出虚假确定性。
    for (speaker, target), team in previous_checks.items():
        if speaker == view.player_id:
            continue
        trust = 0.1 if speaker in contradictions else 0.65
        if team is Team.WEREWOLF:
            add(target, weights.accusation * trust, f"玩家 {speaker} 声称查验其为狼人，这项声明仍需交叉验证。")
        else:
            add(target, -weights.defense * trust, f"玩家 {speaker} 声称查验其为好人，需要同时核对声明者可信度。")
    for voter, reason in deviations:
        add(voter, weights.vote_deviation, reason)

    current_intents = {
        speaker: target for speaker, (target, day) in intents.items()
        if day == view.round_number and speaker in alive and target in alive and speaker != target
    }
    for target in current_intents.values():
        add(target, weights.public_pressure, "已有玩家公开表达投票意向，可考虑能否形成放逐多数。")
    for target, team in known.items():
        if target in scores:
            scores[target] = 100.0 if team is Team.WEREWOLF else -100.0
            reasons[target].append("我的私有查验确认其阵营。" if view.role is Role.SEER else "我的角色信息确认其属于狼人阵营。")
    return StrategyAnalysis(
        assessments=tuple(Assessment(pid, round(score, 3), tuple(reasons[pid])) for pid, score in sorted(scores.items())),
        seer_claimants=tuple(sorted(seers)),
        checks=tuple(checks),
        vote_intents=tuple(sorted(current_intents.items())),
    )
