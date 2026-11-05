import importlib.util
from pathlib import Path
import unittest


FROZEN_PATH = Path(__file__).with_name("test_codex_load_check_acceptance.py")
SPEC = importlib.util.spec_from_file_location("load_check_fixtures", FROZEN_PATH)
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


def own_turn_event(kind, start, end, root_turn, turn):
    payload = {"type": kind, "root_turn_id": root_turn, "turn_id": turn}
    payload["started_at"] = int(start)
    if kind != "task_started":
        payload["completed_at"] = int(end)
    if kind == "turn_aborted":
        payload["reason"] = "interrupted"
    return FIXTURES.event_record("event_msg", end if kind != "task_started" else start, payload)


class ClosedHelperIntervals(unittest.TestCase):
    def assert_no_helper_idle_or_stagnation(self, fixture, helper):
        result, _, _ = fixture.run()
        self.assertNotEqual(2, result.returncode, result.stderr + result.stdout)
        helper_signals = [line for line in result.stdout.splitlines()
                          if line.startswith((f"SIGNAL 2а {helper}:", f"SIGNAL 3б {helper}:"))]
        self.assertEqual([], helper_signals, result.stdout)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual("OK\n", result.stdout)

    def test_parent_completion_closes_old_unmatched_turn(self):
        fixture = FIXTURES.Fixture("fix1-parent-completed", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = fixture.now
        helper_started = now - 170 * 60
        parent_events = [
            FIXTURES.subagent_event(leader, helper_started, "started", helper),
            FIXTURES.subagent_event(leader, now - 24 * 60, "completed", helper),
            FIXTURES.assistant_event(leader, now - 60, "Руководитель продолжает работу"),
        ]
        fixture.add_run("leader", "main", leader, age_minutes=1, live_pid=True,
                        events=parent_events, pid=42001)
        old_turn = "helper-old-turn"
        new_turn = "helper-new-turn"
        helper_events = [
            own_turn_event("task_started", helper_started, helper_started, "leader-turn", old_turn),
            own_turn_event("turn_aborted", helper_started, now - 45 * 60, "leader-turn", old_turn),
            own_turn_event("task_started", now - 35 * 60, now - 35 * 60, "leader-turn", new_turn),
            own_turn_event("task_complete", now - 35 * 60, now - 25 * 60, "leader-turn", new_turn),
        ]
        fixture.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        self.assert_no_helper_idle_or_stagnation(fixture, helper)

    def test_turn_aborted_closes_helper_after_parent_interaction(self):
        fixture = FIXTURES.Fixture("fix1-turn-aborted", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = fixture.now
        helper_started = now - 150 * 60
        parent_events = [
            FIXTURES.subagent_event(leader, helper_started, "started", helper),
            FIXTURES.subagent_event(leader, now - 23 * 60, "interacted", helper),
            FIXTURES.assistant_event(leader, now - 60, "Руководитель продолжает работу"),
        ]
        fixture.add_run("leader", "main", leader, age_minutes=1, live_pid=True,
                        events=parent_events, pid=42001)
        helper_events = [
            own_turn_event("task_started", helper_started, helper_started, "leader-turn", "helper-turn"),
            own_turn_event("turn_aborted", helper_started, now - 22 * 60, "leader-turn", "helper-turn"),
        ]
        fixture.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        self.assert_no_helper_idle_or_stagnation(fixture, helper)

    def test_parent_interaction_does_not_reopen_completed_own_history(self):
        fixture = FIXTURES.Fixture("fix2-parent-interaction-after-complete", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = fixture.now
        started = now - 150 * 60
        parent_events = [
            FIXTURES.subagent_event(leader, started, "started", helper),
            FIXTURES.subagent_event(leader, now - 24 * 60, "completed", helper),
            FIXTURES.subagent_event(leader, now - 23 * 60, "interacted", helper),
            FIXTURES.assistant_event(leader, now - 60, "Руководитель продолжает работу"),
        ]
        fixture.add_run("leader", "main", leader, age_minutes=1, live_pid=True,
                        events=parent_events, pid=42001)
        turn = "helper-completed-turn"
        helper_events = [
            own_turn_event("task_started", started, started, "leader-turn", turn),
            FIXTURES.file_change_event(helper, now - 29 * 60, fixture.root / "outside.py",
                                       turn_id=turn),
            FIXTURES.command_event(helper, now - 28 * 60, "pytest", 1, "", "fix2-command-1"),
            FIXTURES.command_event(helper, now - 27 * 60, "pytest", 1, "", "fix2-command-2"),
            FIXTURES.command_event(helper, now - 26 * 60, "pytest", 1, "", "fix2-command-3"),
            own_turn_event("task_complete", started, now - 25 * 60, "leader-turn", turn),
        ]
        fixture.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        result, _, _ = fixture.run()
        self.assertNotEqual(2, result.returncode, result.stderr + result.stdout)
        self.assertEqual("OK\n", result.stdout)
        self.assertEqual(0, result.returncode, result.stdout)

    def test_own_turn_started_after_parent_interaction_is_live(self):
        fixture = FIXTURES.Fixture("fix2-own-resume-after-parent-interaction", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = fixture.now
        started = now - 50 * 60
        parent_events = [
            FIXTURES.subagent_event(leader, started, "started", helper),
            FIXTURES.subagent_event(leader, now - 40 * 60, "completed", helper),
            FIXTURES.subagent_event(leader, now - 30 * 60, "interacted", helper),
            FIXTURES.assistant_event(leader, now - 60, "Руководитель продолжает работу"),
        ]
        fixture.add_run("leader", "main", leader, age_minutes=1, live_pid=True,
                        events=parent_events, pid=42001)
        helper_events = [
            own_turn_event("task_started", started, started, "leader-turn", "helper-first-turn"),
            own_turn_event("task_complete", started, now - 40 * 60, "leader-turn", "helper-first-turn"),
            own_turn_event("task_started", now - 22 * 60, now - 22 * 60,
                           "leader-turn", "helper-resumed-turn"),
        ]
        rollout = fixture.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        result, _, _ = fixture.run()
        self.assertEqual(3, result.returncode, result.stderr + result.stdout)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines), result.stdout)
        self.assertRegex(lines[0],
                         rf"^SIGNAL 2а {helper}: простой; без событий=\d+\.\d{{2}} мин; "
                         rf"порог=20\.00 мин; источник={rollout}:4$")
