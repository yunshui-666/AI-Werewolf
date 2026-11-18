"""策略的依据必须来自公开信号或自己的角色信息。

从可控公开事件构造局面，验证对跳、查验声称和投票承诺会改变决策。通过改变不可见角色验证策略没有读取主持人真实底牌。
"""

from dataclasses import replace
import unittest

from agents.memory import InspectionResult, PublicEvent, Team
from agents.player import Action, MockAgent
from agents.strategy import StrategyWeights, analyze_view
from config import GameConfig
from game.runner import GameRunner
from game.state import Personality, Phase, Role
from workflow.graph import run_graph


def speech(actor: int, text: str, day: int = 1) -> PublicEvent:
    return PublicEvent(day, "speech", f"玩家 {actor}：{text}", actor_id=actor)


class StrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = GameRunner(GameConfig(seed=42))
        self.view = self.runner.engine.player_view(1)

    def test_check_claim_changes_vote_and_is_still_an_uncertain_signal(self) -> None:
        view = replace(self.view, public_events=(speech(2, "我是预言家，我查验玩家 3 为狼人。我准备投玩家 3。"),))
        analysis = analyze_view(view)
        self.assertEqual(analysis.seer_claimants, (2,))
        self.assertGreater(analysis.score(3), 0)
        self.assertLess(analysis.score(3), 100)
        decision = MockAgent(42).decide(view, Action.VOTE, (2, 3, 4))
        self.assertEqual(decision.target_id, 3)

    def test_repeating_same_claim_does_not_accumulate_certainty(self) -> None:
        event = speech(2, "我是预言家，我查验玩家 3 为狼人。")
        a = analyze_view(replace(self.view, public_events=(event,)))
        b = analyze_view(replace(self.view, public_events=(event, event, event)))
        self.assertEqual(a.score(3), b.score(3))

    def test_contradictory_checks_reduce_claimants_credibility(self) -> None:
        view = replace(self.view, public_events=(
            speech(2, "我是预言家，我查验玩家 3 为狼人。"),
            speech(2, "我查验玩家 3 为好人。"),
        ))
        analysis = analyze_view(view)
        self.assertGreater(analysis.score(2), analysis.score(3))
        self.assertEqual(MockAgent(1).decide(view, Action.VOTE, (2, 3, 4)).target_id, 2)

    def test_false_check_against_self_can_be_challenged(self) -> None:
        view = replace(self.view, public_events=(speech(2, "我是预言家，我查验玩家 1 为狼人。"),))
        self.assertGreater(analyze_view(view).score(2), 4)

    def test_two_seer_claims_are_compared(self) -> None:
        view = replace(self.view, public_events=(
            speech(2, "我是预言家，我查验玩家 4 为好人。"),
            speech(3, "我才是预言家，我查验玩家 4 为狼人。"),
        ))
        self.assertEqual(analyze_view(view).seer_claimants, (2, 3))

    def test_declared_vote_is_compared_with_settled_vote(self) -> None:
        events = (
            speech(2, "我准备投玩家 3。"),
            PublicEvent(1, "vote_result", "票型", votes=((2, 4),)),
        )
        analysis = analyze_view(replace(self.view, public_events=events))
        self.assertGreater(analysis.score(2), 0)
        self.assertIn("实际选票", analysis.reason(2))

    def test_latest_explained_vote_intent_replaces_previous_intent(self) -> None:
        events = (
            speech(2, "我准备投玩家 3。"),
            speech(2, "玩家 3 解释后，我准备投玩家 4。"),
            PublicEvent(1, "vote_result", "票型", votes=((2, 4),)),
        )
        analysis = analyze_view(replace(self.view, public_events=events))
        self.assertEqual(analysis.score(2), 0)
        self.assertEqual(dict(analysis.vote_intents)[2], 4)

    def test_real_private_inspection_has_priority_over_public_lie(self) -> None:
        seer = next(pid for pid, p in self.runner.engine.state.players.items() if p.role is Role.SEER)
        view = replace(
            self.runner.engine.player_view(seer),
            inspections=(InspectionResult(1, 3, Team.GOOD), InspectionResult(1, 7, Team.WEREWOLF)),
            public_events=(speech(2, "我是预言家，我查验玩家 3 为狼人。"),),
        )
        analysis = analyze_view(view)
        self.assertEqual(analysis.score(3), -100)
        self.assertEqual(analysis.score(7), 100)
        self.assertEqual(MockAgent(42).decide(view, Action.VOTE, (2, 3, 7, 8)).target_id, 7)

    def test_wolf_bluff_is_consistent_and_targets_an_opponent(self) -> None:
        wolf = self.runner.engine.state.alive_wolf_ids[0]
        view = self.runner.engine.player_view(wolf)
        candidates = tuple(p.player_id for p in view.players if p.player_id != wolf)
        agent = MockAgent(42)
        first = agent.decide(view, Action.SPEAK, candidates)
        self.assertIn("我是预言家", first.text)
        self.assertNotIn(first.target_id, view.wolf_teammates)
        later_view = replace(view, public_events=(speech(wolf, first.text),), discussion_round=2)
        second = agent.decide(later_view, Action.SPEAK, candidates)
        vote = agent.decide(later_view, Action.VOTE, candidates)
        self.assertEqual(first.target_id, second.target_id)
        self.assertEqual(first.target_id, vote.target_id)

    def test_wolf_targets_publicly_claimed_seer_without_true_role_access(self) -> None:
        view = self.runner.engine.player_view(7)
        view = replace(view, public_events=(speech(2, "我是预言家。"),))
        result = MockAgent(42).decide(view, Action.WOLF_KILL, (1, 2, 3, 4, 5, 6))
        self.assertEqual(result.target_id, 2)

    def test_wolf_follows_teammates_private_kill_choice(self) -> None:
        view = self.runner.engine.player_view(8)
        view = replace(view, private_notes=("第 1 夜，狼人队友 7 提议击杀玩家 4。",))
        result = MockAgent(42).decide(view, Action.WOLF_KILL, (1, 2, 3, 4, 5, 6))
        self.assertEqual(result.target_id, 4)

    def test_logical_wolf_can_bus_teammate_under_public_majority(self) -> None:
        view = self.runner.engine.player_view(8)
        events = tuple(speech(pid, "我准备投玩家 7。") for pid in range(1, 6))
        view = replace(view, personality=Personality.LOGICAL, public_events=events)
        result = MockAgent(42).decide(view, Action.VOTE, (1, 2, 3, 4, 5, 6, 7))
        self.assertEqual(result.target_id, 7)

    def test_changing_hidden_roles_does_not_change_villagers_analysis(self) -> None:
        first = analyze_view(self.view)
        self.runner.engine.state.players[2].role = Role.WEREWOLF
        self.runner.engine.state.players[7].role = Role.VILLAGER
        second = analyze_view(self.runner.engine.player_view(1))
        self.assertEqual(first, second)

    def test_weights_can_change_response_to_vote_deviation(self) -> None:
        view = replace(self.view, public_events=(
            speech(2, "我准备投玩家 3。"),
            PublicEvent(1, "vote_result", "票型", votes=((2, 4),)),
        ))
        a = analyze_view(view, StrategyWeights(vote_deviation=1))
        b = analyze_view(view, StrategyWeights(vote_deviation=4))
        self.assertEqual(b.score(2) - a.score(2), 3)

    def test_two_round_strategy_games_finish_without_fallback(self) -> None:
        for seed in range(30):
            with self.subTest(seed=seed):
                runner = GameRunner(GameConfig(seed=seed, discussion_rounds=2))
                self.assertIs(run_graph(runner).phase, Phase.FINISHED)
                self.assertEqual(runner.fallback_count, 0)
