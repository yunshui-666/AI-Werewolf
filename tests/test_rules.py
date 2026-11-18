"""通过引擎正常入口测试规则，使用 unittest 保持第一阶段零依赖。

规则测试按准备状态、提交行动、检查结果的顺序组织。EngineTestCase 提供夜晚、讨论和投票助手，非法动作通过深拷贝前后比较验证原子性。
"""

from collections import Counter
from copy import deepcopy
import unittest

from config import GameConfig
from game.engine import GameEngine
from game.rules import InvalidAction, check_outcome
from game.state import Outcome, Phase, Role


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = GameEngine(GameConfig(seed=42))
        self.state = self.engine.state
        self.wolves = list(self.state.alive_wolf_ids)
        self.seer = next(pid for pid, p in self.state.players.items() if p.role is Role.SEER)
        self.villagers = [pid for pid, p in self.state.players.items() if p.role is Role.VILLAGER]

    def reach_day(self) -> None:
        victim = next(pid for pid in reversed(self.villagers) if self.state.players[pid].alive)
        for wolf in self.state.alive_wolf_ids:
            self.engine.submit_wolf_choice(wolf, victim)
        if self.state.players[self.seer].alive:
            self.engine.inspect(self.seer, self.state.alive_wolf_ids[0])
        self.engine.resolve_night()

    def reach_voting(self) -> None:
        self.reach_day()
        for pid in self.state.alive_ids:
            self.engine.speak(pid, "继续观察其他玩家的发言。")

    def submit_tie(self) -> None:
        alive = self.state.alive_ids
        for index, pid in enumerate(alive):
            self.engine.cast_vote(pid, alive[(index + 1) % len(alive)])

    def submit_exile(self, target_id: int) -> None:
        other = next(pid for pid in self.state.alive_ids if pid != target_id)
        for pid in self.state.alive_ids:
            self.engine.cast_vote(pid, other if pid == target_id else target_id)

    def assert_rejected_without_change(self, action, *args) -> None:
        before = deepcopy(self.state)
        with self.assertRaises(InvalidAction):
            action(*args)
        self.assertEqual(self.state, before)


class InitializationTests(EngineTestCase):
    def test_exact_roles_and_unique_ids_across_seeds(self) -> None:
        for seed in range(30):
            with self.subTest(seed=seed):
                state = GameEngine(GameConfig(seed=seed)).state
                self.assertEqual(state.alive_ids, tuple(range(1, 9)))
                self.assertEqual(Counter(p.role for p in state.players.values()), {
                    Role.WEREWOLF: 2, Role.SEER: 1, Role.VILLAGER: 5,
                })
                self.assertEqual(state.round_number, 1)
                self.assertIs(state.phase, Phase.NIGHT)
                self.assertIsNone(state.outcome)

    def test_seed_reproduces_roles_and_personalities(self) -> None:
        other = GameEngine(GameConfig(seed=42))
        self.assertEqual(self.state, other.state)

    def test_invalid_round_limit(self) -> None:
        for value in (0, -1, True, 1.5, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GameConfig(max_rounds=value)


class NightTests(EngineTestCase):
    def test_captain_decides_when_wolves_disagree(self) -> None:
        captain, teammate = self.wolves
        self.engine.submit_wolf_choice(teammate, self.villagers[1])
        self.engine.submit_wolf_choice(captain, self.villagers[0])
        self.engine.inspect(self.seer, teammate)
        victim = self.engine.resolve_night()
        self.assertEqual(victim, self.villagers[0])
        self.assertFalse(self.state.players[victim].alive)
        self.assertTrue(self.state.players[self.villagers[1]].alive)
        self.assertIs(self.state.phase, Phase.DAY)

    def test_surviving_wolf_becomes_captain(self) -> None:
        self.state.players[self.wolves[0]].alive = False
        self.engine.submit_wolf_choice(self.wolves[1], self.villagers[0])
        self.engine.inspect(self.seer, self.wolves[1])
        self.assertEqual(self.engine.resolve_night(), self.villagers[0])

    def test_reject_illegal_wolf_choices(self) -> None:
        captain, teammate = self.wolves
        for actor, target in (
            (self.villagers[0], self.villagers[1]),
            (self.seer, self.villagers[0]),
            (captain, captain),
            (captain, teammate),
            (captain, 999),
            (999, self.villagers[0]),
            (True, self.villagers[0]),
            (float(captain), self.villagers[0]),
            (captain, "1"),
        ):
            with self.subTest(actor=actor, target=target):
                self.assert_rejected_without_change(self.engine.submit_wolf_choice, actor, target)

    def test_dead_players_cannot_kill_or_be_targeted(self) -> None:
        wolf, target = self.wolves[0], self.villagers[0]
        self.state.players[target].alive = False
        self.assert_rejected_without_change(self.engine.submit_wolf_choice, wolf, target)
        self.state.players[wolf].alive = False
        self.assert_rejected_without_change(self.engine.submit_wolf_choice, wolf, self.villagers[1])

    def test_duplicate_wolf_choice_is_rejected(self) -> None:
        wolf = self.wolves[0]
        self.engine.submit_wolf_choice(wolf, self.villagers[0])
        self.assert_rejected_without_change(self.engine.submit_wolf_choice, wolf, self.villagers[1])

    def test_night_requires_all_living_actions(self) -> None:
        self.assert_rejected_without_change(self.engine.resolve_night)
        self.engine.submit_wolf_choice(self.wolves[0], self.villagers[0])
        self.assert_rejected_without_change(self.engine.resolve_night)
        self.engine.submit_wolf_choice(self.wolves[1], self.villagers[0])
        self.assert_rejected_without_change(self.engine.resolve_night)

    def test_dead_seer_does_not_block_night(self) -> None:
        self.state.players[self.seer].alive = False
        for wolf in self.wolves:
            self.engine.submit_wolf_choice(wolf, self.villagers[0])
        self.assertEqual(self.engine.resolve_night(), self.villagers[0])

    def test_seer_targeted_tonight_still_inspects(self) -> None:
        for wolf in self.wolves:
            self.engine.submit_wolf_choice(wolf, self.seer)
        result = self.engine.inspect(self.seer, self.wolves[0])
        self.assertEqual(result.team, Role.WEREWOLF.team)
        self.engine.resolve_night()
        self.assertFalse(self.state.players[self.seer].alive)
        self.assertEqual(self.state.players[self.seer].memory.inspections, [result])

    def test_inspection_returns_team_only(self) -> None:
        result = self.engine.inspect(self.seer, self.villagers[0])
        self.assertEqual(result.team, Role.VILLAGER.team)
        self.assertFalse(hasattr(result, "role"))
        self.assert_rejected_without_change(self.engine.inspect, self.seer, self.wolves[0])

    def test_inspection_requires_living_seer_and_legal_target(self) -> None:
        for actor, target in (
            (self.wolves[0], self.villagers[0]),
            (self.villagers[0], self.wolves[0]),
            (self.seer, self.seer),
            (self.seer, 999),
        ):
            with self.subTest(actor=actor, target=target):
                self.assert_rejected_without_change(self.engine.inspect, actor, target)
        self.state.players[self.villagers[0]].alive = False
        self.assert_rejected_without_change(self.engine.inspect, self.seer, self.villagers[0])
        self.state.players[self.seer].alive = False
        self.assert_rejected_without_change(self.engine.inspect, self.seer, self.wolves[0])

    def test_night_cannot_be_resolved_twice(self) -> None:
        self.reach_day()
        self.assert_rejected_without_change(self.engine.resolve_night)


class DayAndVotingTests(EngineTestCase):
    def test_speech_order_and_voting_transition(self) -> None:
        self.reach_day()
        alive = self.state.alive_ids
        self.assert_rejected_without_change(self.engine.speak, alive[1], "抢先发言")
        self.assert_rejected_without_change(self.engine.speak, alive[0], "  ")
        self.assert_rejected_without_change(self.engine.speak, alive[0], None)
        self.engine.speak(alive[0], "我的第一条发言")
        self.assert_rejected_without_change(self.engine.speak, alive[0], "重复发言")
        for pid in alive[1:]:
            self.engine.speak(pid, "尚需更多信息。")
        self.assertIs(self.state.phase, Phase.VOTING)
        self.assertEqual(self.state.speakers, list(alive))
        self.assert_rejected_without_change(self.engine.speak, alive[0], "再次发言")

    def test_vote_requires_other_living_target_and_single_vote(self) -> None:
        self.reach_voting()
        actor, target = self.state.alive_ids[:2]
        dead = next(pid for pid, player in self.state.players.items() if not player.alive)
        for voter, candidate in ((actor, actor), (actor, dead), (dead, actor), (actor, 999)):
            with self.subTest(voter=voter, candidate=candidate):
                self.assert_rejected_without_change(self.engine.cast_vote, voter, candidate)
        self.engine.cast_vote(actor, target)
        self.assert_rejected_without_change(self.engine.cast_vote, actor, target)

    def test_dead_player_cannot_speak(self) -> None:
        self.reach_day()
        dead = next(pid for pid, player in self.state.players.items() if not player.alive)
        self.assert_rejected_without_change(self.engine.speak, dead, "我已经死亡")

    def test_highest_vote_is_exiled_and_next_night_is_clean(self) -> None:
        self.reach_voting()
        target = self.wolves[0]
        self.submit_exile(target)
        result = self.engine.resolve_votes()
        self.assertEqual(result.eliminated_id, target)
        self.assertEqual(dict(result.counts)[target], 6)
        self.assertFalse(self.state.players[target].alive)
        self.assertIs(self.state.phase, Phase.NIGHT)
        self.assertEqual(self.state.round_number, 2)
        self.assertEqual(self.state.wolf_choices, {})
        self.assertFalse(self.state.seer_acted)
        self.assertEqual(self.state.speakers, [])
        self.assertEqual(self.state.votes, {})
        self.assertEqual(self.engine.player_view(self.seer).inspections[0].round_number, 1)

    def test_highest_vote_tie_eliminates_nobody(self) -> None:
        self.reach_voting()
        before = self.state.alive_ids
        self.submit_tie()
        result = self.engine.resolve_votes()
        self.assertIsNone(result.eliminated_id)
        self.assertEqual(result.tied_ids, before)
        self.assertEqual(self.state.alive_ids, before)

    def test_partial_top_tie_with_lower_counts_eliminates_nobody(self) -> None:
        self.reach_voting()
        a, b, c, d, e, f, g = self.state.alive_ids
        votes = {a: b, b: a, c: a, d: a, e: b, f: b, g: c}
        for actor, target in votes.items():
            self.engine.cast_vote(actor, target)
        result = self.engine.resolve_votes()
        self.assertEqual(result.tied_ids, (a, b))
        self.assertIsNone(result.eliminated_id)

    def test_incomplete_vote_cannot_be_resolved(self) -> None:
        self.reach_voting()
        self.assert_rejected_without_change(self.engine.resolve_votes)
        actor, target = self.state.alive_ids[:2]
        self.engine.cast_vote(actor, target)
        self.assert_rejected_without_change(self.engine.resolve_votes)

    def test_actions_in_wrong_phase_are_rejected(self) -> None:
        a, b = self.villagers[:2]
        self.assert_rejected_without_change(self.engine.speak, a, "夜间发言")
        self.assert_rejected_without_change(self.engine.cast_vote, a, b)
        self.assert_rejected_without_change(self.engine.resolve_votes)
        self.reach_day()
        self.assert_rejected_without_change(self.engine.submit_wolf_choice, self.wolves[0], a)
        self.assert_rejected_without_change(self.engine.inspect, self.seer, self.wolves[0])
        self.assert_rejected_without_change(self.engine.cast_vote, a, b)


class OutcomeTests(EngineTestCase):
    def test_game_starts_without_winner(self) -> None:
        self.assertIsNone(check_outcome(self.state))

    def test_all_wolves_dead_means_good_win(self) -> None:
        for wolf in self.wolves:
            self.state.players[wolf].alive = False
        self.assertIs(check_outcome(self.state), Outcome.GOOD)

    def test_parity_and_wolf_majority_win(self) -> None:
        for pid in self.villagers[:4]:
            self.state.players[pid].alive = False
        self.assertIs(check_outcome(self.state), Outcome.WEREWOLF)
        self.state.players[self.seer].alive = False
        self.assertIs(check_outcome(self.state), Outcome.WEREWOLF)

    def test_wolf_win_checked_immediately_after_night(self) -> None:
        for pid in self.villagers[:3]:
            self.state.players[pid].alive = False
        self.reach_day()
        self.assertIs(self.state.outcome, Outcome.WEREWOLF)
        self.assertIs(self.state.phase, Phase.FINISHED)
        self.assertFalse(any(e.kind == "day_started" for e in self.state.public_events))

    def test_wolf_win_checked_immediately_after_exile(self) -> None:
        for pid in self.villagers[:2]:
            self.state.players[pid].alive = False
        self.reach_voting()
        self.submit_exile(self.villagers[2])
        self.engine.resolve_votes()
        self.assertIs(self.state.outcome, Outcome.WEREWOLF)
        self.assertIs(self.state.phase, Phase.FINISHED)

    def test_round_limit_draw_after_complete_day(self) -> None:
        self.state.max_rounds = 1
        self.reach_voting()
        self.assertIsNone(self.state.outcome)
        self.submit_tie()
        self.engine.resolve_votes()
        self.assertIs(self.state.outcome, Outcome.DRAW)
        self.assertEqual(self.state.round_number, 1)
        self.assertIs(self.state.phase, Phase.FINISHED)

    def test_victory_takes_precedence_over_round_limit(self) -> None:
        self.state.max_rounds = 1
        self.state.players[self.wolves[0]].alive = False
        self.reach_voting()
        self.submit_exile(self.wolves[1])
        self.engine.resolve_votes()
        self.assertIs(self.state.outcome, Outcome.GOOD)

    def test_reveal_and_all_actions_after_game_finished(self) -> None:
        self.assert_rejected_without_change(self.engine.final_roles)
        self.state.max_rounds = 1
        self.reach_voting()
        self.submit_tie()
        self.engine.resolve_votes()
        self.assertEqual(self.engine.final_roles(), {pid: p.role for pid, p in self.state.players.items()})
        self.assert_rejected_without_change(self.engine.submit_wolf_choice, self.wolves[0], self.seer)
        self.assert_rejected_without_change(self.engine.inspect, self.seer, self.wolves[0])
        self.assert_rejected_without_change(self.engine.speak, self.seer, "发言")
        self.assert_rejected_without_change(self.engine.cast_vote, self.seer, self.wolves[0])
        self.assert_rejected_without_change(self.engine.resolve_night)
        self.assert_rejected_without_change(self.engine.resolve_votes)


class ScriptedGameTests(EngineTestCase):
    """使用明确指定的行动验证多轮衔接，第二阶段再添加自动决策 Agent。"""

    def test_two_round_good_victory_through_normal_actions(self) -> None:
        self.reach_voting()
        self.submit_exile(self.wolves[0])
        self.engine.resolve_votes()
        self.assertEqual(self.state.round_number, 2)
        self.reach_voting()
        self.submit_exile(self.wolves[1])
        self.engine.resolve_votes()
        self.assertIs(self.state.outcome, Outcome.GOOD)
        self.assertIs(self.state.phase, Phase.FINISHED)
        self.assertEqual(len(self.engine.final_roles()), 8)
        self.assertEqual(len(self.state.players[self.seer].memory.inspections), 2)
        self.assertEqual(len([e for e in self.state.public_events if e.kind == "vote_result"]), 2)

    def test_tied_votes_eventually_lead_to_wolf_victory(self) -> None:
        for expected_round in range(1, 5):
            self.assertEqual(self.state.round_number, expected_round)
            self.reach_day()
            if self.state.phase is Phase.FINISHED:
                break
            for pid in self.state.alive_ids:
                self.engine.speak(pid, "暂时无法统一意见。")
            self.submit_tie()
            self.engine.resolve_votes()
        self.assertIs(self.state.outcome, Outcome.WEREWOLF)
        self.assertEqual(self.state.round_number, 4)
        self.assertEqual(len(self.state.alive_ids), 4)


if __name__ == "__main__":
    unittest.main()
