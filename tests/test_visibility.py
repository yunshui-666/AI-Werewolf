"""确保私有状态只进入所属玩家视图，公开日志不会泄露自动行动信息。

信息隔离测试直接检查玩家快照和各自记忆容器。公开声称可以真假混合，主持人注入的私有结果必须保持本人可见。
"""

from dataclasses import FrozenInstanceError, asdict
import json
import unittest

from game.state import Role
from tests.test_rules import EngineTestCase


class VisibilityTests(EngineTestCase):
    def test_each_player_has_independent_memory_containers(self) -> None:
        memories = [player.memory for player in self.state.players.values()]
        for field in (None, "public_events", "private_notes", "inspections", "suspicions"):
            objects = [getattr(memory, field) if field else memory for memory in memories]
            self.assertEqual(len({id(obj) for obj in objects}), 8)

    def test_view_exposes_only_own_role(self) -> None:
        for pid, player in self.state.players.items():
            with self.subTest(player=pid):
                view = self.engine.player_view(pid)
                self.assertIs(view.role, player.role)
                self.assertEqual(len(view.players), 8)
                for visible in view.players:
                    self.assertEqual(set(asdict(visible)), {"player_id", "alive"})
                expected = tuple(wolf for wolf in self.wolves if wolf != pid)
                self.assertEqual(view.wolf_teammates, expected if player.role is Role.WEREWOLF else ())
                self.assertFalse(hasattr(view, "wolf_choices"))
                self.assertFalse(hasattr(view, "votes"))

    def test_inspection_is_visible_only_to_seer(self) -> None:
        self.state.players[self.seer].memory.private_notes.append("SEER_PRIVATE_MARKER")
        result = self.engine.inspect(self.seer, self.wolves[0])
        for pid in self.state.players:
            view = self.engine.player_view(pid)
            if pid == self.seer:
                self.assertEqual(view.inspections, (result,))
            else:
                self.assertEqual(view.inspections, ())
                serialized = json.dumps(asdict(view), ensure_ascii=False)
                self.assertNotIn("SEER_PRIVATE_MARKER", serialized)

    def test_wolf_choice_is_visible_only_to_wolves(self) -> None:
        wolf, target = self.wolves[0], self.villagers[0]
        self.engine.submit_wolf_choice(wolf, target)
        for pid, player in self.state.players.items():
            notes = self.engine.player_view(pid).private_notes
            choices = [note for note in notes if "提议击杀" in note]
            self.assertEqual(len(choices), 1 if player.role is Role.WEREWOLF else 0)

    def test_suspicions_are_private_and_snapshot_is_detached(self) -> None:
        owner, other = self.villagers[:2]
        memory = self.state.players[owner].memory
        memory.suspicions[self.wolves[0]] = "PRIVATE_SUSPICION"
        view = self.engine.player_view(owner)
        self.assertEqual(view.suspicions, ((self.wolves[0], "PRIVATE_SUSPICION"),))
        self.assertEqual(self.engine.player_view(other).suspicions, ())
        memory.suspicions.clear()
        memory.private_notes.append("LATER_PRIVATE_NOTE")
        self.assertEqual(len(view.suspicions), 1)
        self.assertNotIn("LATER_PRIVATE_NOTE", view.private_notes)

    def test_view_and_public_events_are_immutable(self) -> None:
        view = self.engine.player_view(self.villagers[0])
        with self.assertRaises(FrozenInstanceError):
            view.role = Role.WEREWOLF
        with self.assertRaises(FrozenInstanceError):
            view.players[0].alive = False
        with self.assertRaises(FrozenInstanceError):
            view.public_events[0].text = "覆盖公开事件"

    def test_pending_votes_are_not_public(self) -> None:
        self.reach_voting()
        actor, target = self.state.alive_ids[:2]
        events = tuple(self.state.public_events)
        self.engine.cast_vote(actor, target)
        self.assertEqual(tuple(self.state.public_events), events)
        for pid in self.state.alive_ids:
            self.assertFalse(any(e.kind == "vote_result" for e in self.engine.player_view(pid).public_events))

    def test_settled_vote_history_is_copied_to_all_memories(self) -> None:
        self.reach_voting()
        self.submit_tie()
        expected = tuple(sorted(self.state.votes.items()))
        self.engine.resolve_votes()
        for pid in self.state.players:
            view = self.engine.player_view(pid)
            vote_event = next(e for e in view.public_events if e.kind == "vote_result")
            self.assertEqual(vote_event.votes, expected)
            self.assertEqual(view.public_events, tuple(self.state.public_events))

    def test_public_night_logs_have_no_roles_or_private_actions(self) -> None:
        with self.assertLogs("werewolf_arena.public", level="INFO") as captured:
            self.reach_day()
        public_text = "\n".join(captured.output)
        self.assertNotIn("查验", public_text)
        self.assertNotIn("提议", public_text)
        for role in ("狼人", "预言家", "村民", "werewolf", "seer", "villager"):
            self.assertNotIn(role, "\n".join(e.text for e in self.state.public_events))
        deaths = [e for e in self.state.public_events if e.kind == "night_death"]
        self.assertEqual(len(deaths), 1)
        self.assertIsNone(deaths[0].actor_id)
        self.assertEqual(deaths[0].votes, ())

    def test_seer_can_voluntarily_disclose_information_in_speech(self) -> None:
        self.reach_day()
        disclosure = "我是预言家，我查验了狼人。"
        for pid in self.state.alive_ids:
            self.engine.speak(pid, disclosure if pid == self.seer else "继续观察。")
        for pid in self.state.players:
            self.assertTrue(any(disclosure in event.text for event in self.engine.player_view(pid).public_events))

    def test_clearing_one_memory_does_not_change_others(self) -> None:
        first, second = self.villagers[:2]
        self.state.players[first].memory.public_events.clear()
        self.assertEqual(len(self.engine.player_view(first).public_events), 0)
        self.assertEqual(len(self.engine.player_view(second).public_events), 2)
        self.assertEqual(len(self.state.public_events), 2)


if __name__ == "__main__":
    unittest.main()
