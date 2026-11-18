"""12 人角色交互的确定性回归。

直接指定合法行动来隔离策略随机性，重点覆盖药品消耗、同守同救、死亡猎人
响应与信息可见性。旧 8 人规则仍由原有 tests/test_rules.py 覆盖。
"""

from collections import Counter
from copy import deepcopy
import unittest

from agents.player import Action, MockAgent
from agents.prompts import build_messages
from agents.skills import load_role_skill
from config import GameConfig
from game.engine import GameEngine
from game.rules import InvalidAction, check_outcome
from game.runner import GameRunner
from game.state import Outcome, Phase, Role
from workflow.graph import run_graph


class ExpandedRulesTests(unittest.TestCase):
    """每项测试获得独立的完整 12 人局，避免技能资源跨用例共享。"""

    def setUp(self):
        self.engine = GameEngine(GameConfig(seed=42, preset="standard12"))
        self.state = self.engine.state
        self.by_role = {
            role: [pid for pid, player in self.state.players.items() if player.role is role]
            for role in Role
        }
        self.witch = self.by_role[Role.WITCH][0]
        self.guard = self.by_role[Role.GUARD][0]
        self.seer = self.by_role[Role.SEER][0]
        self.hunter = self.by_role[Role.HUNTER][0]
        self.villagers = self.by_role[Role.VILLAGER]

    def prepare_night(self, victim=None, guard=None, potion="pass", potion_target=None):
        """按照合法先后依赖提交技能，保持所有角色同时在夜末结算。"""
        victim = self.villagers[0] if victim is None else victim
        self.engine.guard(self.guard, guard)
        for wolf in self.state.alive_wolf_ids:
            self.engine.submit_wolf_choice(wolf, victim)
        self.engine.witch(self.witch, potion, potion_target)
        self.engine.inspect(self.seer, self.state.alive_wolf_ids[0])
        return victim

    def rejected(self, method, *args):
        """非法行动必须在任何状态修改之前失败。"""
        before = deepcopy(self.state)
        with self.assertRaises(InvalidAction):
            method(*args)
        self.assertEqual(self.state, before)

    def test_distribution_and_role_skills(self):
        counts = Counter(player.role for player in self.state.players.values())
        self.assertEqual(counts[Role.WEREWOLF], 4)
        self.assertEqual(counts[Role.VILLAGER], 4)
        self.assertEqual(len(self.state.players), 12)
        for player in self.state.players.values():
            self.assertEqual(player.role_skill.role, player.role.value)
            self.assertIn("输出纪律", player.role_skill.instructions)

    def test_agents_bind_only_their_assigned_skill(self):
        runner = GameRunner(GameConfig(preset="standard12", seed=42))
        for pid, agent in runner.agents.items():
            self.assertEqual(agent.role_skill.role, runner.engine.state.players[pid].role.value)
            self.assertIs(agent.role_skill, runner.fallback_agents[pid].role_skill)

    def test_guard_alone_prevents_wolf_death(self):
        victim = self.prepare_night(guard=self.villagers[0])
        self.assertEqual(self.engine.resolve_night(), ())
        self.assertTrue(self.state.players[victim].alive)

    def test_antidote_alone_prevents_wolf_death(self):
        victim = self.prepare_night(potion="save", potion_target=self.villagers[0])
        self.assertEqual(self.engine.resolve_night(), ())
        self.assertTrue(self.state.players[victim].alive)
        self.assertFalse(self.state.players[self.witch].antidote_available)

    def test_simultaneous_guard_and_save_still_causes_death(self):
        victim = self.prepare_night(guard=self.villagers[0], potion="save", potion_target=self.villagers[0])
        self.engine.resolve_night()
        self.assertFalse(self.state.players[victim].alive)

    def test_poison_pierces_guard_and_can_kill_another_target(self):
        victim = self.prepare_night(guard=self.villagers[1], potion="poison", potion_target=self.villagers[1])
        deaths = self.engine.resolve_night()
        self.assertEqual(set(deaths), {victim, self.villagers[1]})

    def test_poison_takes_priority_when_hunter_is_also_wolf_target(self):
        self.prepare_night(victim=self.hunter, potion="poison", potion_target=self.hunter)
        self.engine.resolve_night()
        self.assertEqual(self.state.pending_hunters, [])
        self.assertIs(self.state.phase, Phase.DAY)

    def test_dead_hunter_can_choose_shot_once(self):
        self.prepare_night(victim=self.hunter)
        self.engine.resolve_night()
        self.assertIs(self.state.phase, Phase.HUNTER)
        wolf = self.state.alive_wolf_ids[0]
        self.engine.hunter_shoot(self.hunter, wolf)
        self.assertFalse(self.state.players[wolf].alive)
        self.assertTrue(self.state.players[self.hunter].hunter_shot_used)
        self.rejected(self.engine.hunter_shoot, self.hunter, self.villagers[0])

    def test_hunter_may_abstain(self):
        self.prepare_night(victim=self.hunter)
        self.engine.resolve_night()
        before = self.state.alive_ids
        self.engine.hunter_shoot(self.hunter, None)
        self.assertEqual(self.state.alive_ids, before)
        self.assertIs(self.state.phase, Phase.DAY)

    def test_hunter_shot_precedes_victory_check(self):
        for wolf in self.by_role[Role.WEREWOLF][1:]:
            self.state.players[wolf].alive = False
        for role in (Role.WITCH, Role.GUARD, Role.SEER):
            self.state.players[self.by_role[role][0]].alive = False
        # 最后一名神职被刀，允许他先带走最后一狼，再按最终状态判好人胜利。
        for wolf in self.state.alive_wolf_ids:
            self.engine.submit_wolf_choice(wolf, self.hunter)
        self.engine.resolve_night()
        self.assertIsNone(self.state.outcome)
        self.engine.hunter_shoot(self.hunter, self.state.alive_wolf_ids[0])
        self.assertIs(self.state.outcome, Outcome.GOOD)

    def test_hunter_exile_then_shot_advances_to_next_night(self):
        self.prepare_night()
        self.engine.resolve_night()
        for pid in self.state.alive_ids:
            self.engine.speak(pid, "检查公开记录。")
        for pid in self.state.alive_ids:
            target = self.hunter if pid != self.hunter else self.state.alive_wolf_ids[0]
            self.engine.cast_vote(pid, target)
        self.engine.resolve_votes()
        self.assertIs(self.state.phase, Phase.HUNTER)
        self.engine.hunter_shoot(self.hunter, None)
        self.assertIs(self.state.phase, Phase.NIGHT)
        self.assertEqual(self.state.round_number, 2)

    def test_witch_cannot_self_save_or_use_both_potions(self):
        for wolf in self.state.alive_wolf_ids:
            self.engine.submit_wolf_choice(wolf, self.witch)
        self.rejected(self.engine.witch, self.witch, "save", self.witch)
        self.engine.witch(self.witch, "pass", None)
        self.rejected(self.engine.witch, self.witch, "poison", self.villagers[0])

    def test_guard_can_self_guard_but_not_repeat_target(self):
        self.engine.guard(self.guard, self.guard)
        self.state.guard_acted = False
        self.rejected(self.engine.guard, self.guard, self.guard)
        self.engine.guard(self.guard, None)

    def test_wrong_roles_cannot_use_skills(self):
        self.rejected(self.engine.guard, self.villagers[0], self.villagers[1])
        self.rejected(self.engine.witch, self.villagers[0], "pass", None)
        self.rejected(self.engine.hunter_shoot, self.hunter, self.villagers[0])

    def test_standard12_allows_strategic_wolf_self_attack(self):
        wolf = self.state.alive_wolf_ids[0]
        self.prepare_night(victim=wolf)
        self.engine.resolve_night()
        self.assertFalse(self.state.players[wolf].alive)

    def test_witch_victim_is_private_and_disappears_after_antidote(self):
        for wolf in self.state.alive_wolf_ids:
            self.engine.submit_wolf_choice(wolf, self.villagers[0])
        self.assertEqual(self.engine.player_view(self.witch).night_victim, self.villagers[0])
        for pid in self.state.players:
            if pid != self.witch:
                self.assertIsNone(self.engine.player_view(pid).night_victim)
        self.engine.witch(self.witch, "save", self.villagers[0])
        self.assertIsNone(self.engine.player_view(self.witch).night_victim)

    def test_side_victory_does_not_use_simple_parity(self):
        for pid in self.villagers[:2]:
            self.state.players[pid].alive = False
        for pid in (self.seer, self.witch):
            self.state.players[pid].alive = False
        self.assertEqual(len(self.state.alive_ids), 8)
        self.assertIsNone(check_outcome(self.state))
        for pid in self.villagers[2:]:
            self.state.players[pid].alive = False
        self.assertIs(check_outcome(self.state), Outcome.WEREWOLF)

    def test_prompt_loads_only_corresponding_skill(self):
        view = self.engine.player_view(self.guard)
        text = build_messages(view, Action.GUARD, self.state.alive_ids)[0]["content"]
        self.assertIn(load_role_skill("guard").instructions, text)
        self.assertNotIn(load_role_skill("witch").instructions, text)
        self.assertIn("12 人", text)

    def test_twenty_complete_games_have_no_fallback(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                runner = GameRunner(GameConfig(preset="standard12", seed=seed, discussion_rounds=2))
                self.assertIs(run_graph(runner).phase, Phase.FINISHED)
                self.assertEqual(runner.fallback_count, 0)
