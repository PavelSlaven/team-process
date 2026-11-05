import json
import math
import os
import random
import shutil
import string
import re
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path("/srv/novgorod-work/worktrees/team-process-load")
TMP = ROOT / ".load-check-tests" / "tmp"
CHECKER = ROOT / "tools" / "codex-load-check.py"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
AGE = "@@AGE@@"


def utc_stamp(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def event_record(kind, epoch, payload):
    return {"type": kind, "timestamp": utc_stamp(epoch), "payload": payload}


def rate_event(epoch, primary=None, secondary=None):
    limits = None if primary is None and secondary is None else {"primary": primary, "secondary": secondary}
    return event_record("event_msg", epoch, {
        "type": "token_count",
        "rate_limits": limits,
    })


def assistant_event(thread_id, epoch, text):
    return event_record("response_item", epoch, {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    })


def item_event(thread_id, epoch, item):
    return event_record("event_msg", epoch, {"type": "item_completed", "thread_id": thread_id, "item": item})


def subagent_event(thread_id, epoch, kind, agent_thread_id):
    return item_event(thread_id, epoch, {"type": "SubAgentActivity", "kind": kind,
                                         "agent_thread_id": agent_thread_id})


def task_boundary_event(kind, epoch, turn_id):
    payload = {"type": kind, "turn_id": turn_id}
    payload["started_at" if kind == "task_started" else "completed_at"] = int(epoch)
    return event_record("event_msg", epoch, payload)


def container_exec_event(epoch, call_id):
    return event_record("response_item", epoch, {
        "type": "custom_tool_call", "name": "exec", "call_id": call_id,
        "input": "tools.exec_command({command: 'container operation'})",
    })


def command_event(thread_id, epoch, command, exit_code=0, output="", item_id=None, status=None, cwd=None):
    item = {
        "type": "CommandExecution",
        "command": command,
        "status": status or ("completed" if exit_code == 0 else "failed"),
    }
    if status not in ("started", "running"):
        item["exit_code"] = exit_code
        item["aggregated_output"] = output
    if item_id is not None:
        item["id"] = item_id
    if cwd is not None:
        item["cwd"] = cwd
    return item_event(thread_id, epoch, item)


def file_change_event(thread_id, epoch, path, change_type="add", turn_id="synthetic-turn", item_id="file-change"):
    if change_type == "update":
        change = {"type": "update", "unified_diff": "Синтетический текст.", "move_path": None}
    else:
        change = {"type": change_type, "content": "Синтетический текст."}
    timestamp_ms = int(epoch) * 1000
    return event_record("event_msg", epoch, {
        "type": "item_completed", "thread_id": thread_id, "turn_id": turn_id,
        "item": {"type": "FileChange", "id": item_id, "changes": {str(path): change},
                 "status": "completed", "stdout": "Синтетический текст.", "stderr": "Синтетический текст."},
        "started_at_ms": timestamp_ms, "completed_at_ms": timestamp_ms + 10,
    })


def custom_exec_call_event(epoch, turn_id, call_id, input_text):
    return event_record("response_item", epoch, {
        "type": "custom_tool_call", "id": "call-item-" + call_id, "status": "completed",
        "call_id": call_id, "name": "exec", "input": input_text,
        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id, "create_time": float(epoch)},
    })


def custom_exec_output_event(epoch, turn_id, call_id, first_text, second_text="Синтетический вывод."):
    record = event_record("response_item", epoch, {
        "type": "custom_tool_call_output", "id": "output-item-" + call_id, "call_id": call_id,
        "output": [{"type": "input_text", "text": first_text},
                   {"type": "input_text", "text": second_text}],
        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id, "create_time": float(epoch)},
    })
    record["metadata"] = {"client_authored": False, "fallback_token_limit_override": 12000}
    return record


def allowed_roots_json(paths):
    return json.dumps(sorted(set(paths)), ensure_ascii=False)


def frame_line(thread_id, path, allowed, rollout, source_line):
    return f"SIGNAL 3в {thread_id}: рамка; путь={path}; разрешено={allowed}; источник={rollout}:{source_line}"


def task_meta(session_id, parent_id=None, history_start=None):
    payload = {"id": session_id}
    if parent_id is not None:
        payload["parent_thread_id"] = parent_id
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent_id}}}
    if history_start is not None:
        payload["subagent_history_start_ordinal"] = history_start
    return {"type": "session_meta", "payload": payload}


class Fixture:
    def __init__(self, label, main_slots=0, acct2_slots=0, alphabetic_path=False):
        self.now = int(time.time())
        if alphabetic_path:
            while True:
                suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(16))
                candidate = TMP / (label + "-" + suffix)
                try:
                    candidate.mkdir()
                    self.root = candidate
                    break
                except FileExistsError:
                    continue
        else:
            self.root = Path(tempfile.mkdtemp(prefix=label + "-", dir=TMP))
        self.root = Path(os.path.realpath(self.root))
        self.home = self.root / "home"
        self.tempdir = self.root / "tmpdir"
        self.home.mkdir()
        self.tempdir.mkdir()
        self.tasks = self.root / "tasks"
        self.proc = self.root / "proc"
        self.tasks.mkdir()
        self.proc.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.report = self.root / "report.txt"
        self.signal = self.root / "signal.md"
        self.signal.write_text("existing signal\n", encoding="utf-8")
        self.state = self.root / "STATE.md"
        self.state.write_text(self.state_text([]), encoding="utf-8")
        self.accounts = {}
        self.rollouts = {}
        for name, slots in (("main", main_slots), ("acct2", acct2_slots)):
            home = self.root / "accounts" / name
            (home / "sessions" / "2026" / "11" / "05").mkdir(parents=True)
            self.accounts[name] = {"codex_home": str(home), "slots": slots}
            self.write_quota(name)
        self.config_path = self.root / "config.json"
        self.config = {
            "tasks_dir": str(self.tasks),
            "proc_dir": str(self.proc),
            "accounts": {
                name: {
                    "codex_home": data["codex_home"],
                    "slots": data["slots"],
                    "helper_limit": 60,
                    "plan_deadline": None,
                }
                for name, data in self.accounts.items()
            },
            "plan": {
                "path": str(self.state),
                "columns": {"task": "Задача", "thread": "Нить", "status": "Его статус", "account": None},
                "ready_prefix": "Готова к запуску",
                "closed_prefixes": ["Принята", "Сохранена", "Закрыта"],
            },
            "extra_allowed_paths": ["/tmp"],
            "report_path": str(self.report),
            "signal_path": str(self.signal),
            "thresholds": {
                "window_minutes": 30,
                "rate_snapshot_hours": 6,
                "underuse_pp": 10,
                "idle_minutes": 20,
                "waiting_minutes": 15,
                "repeat_count": 3,
                "stagnation_minutes": 120,
                "data_grace_minutes": 5,
                "leader_command_count": 20,
                "runtime_seconds": 60,
            },
        }
        self.write_config()

    @staticmethod
    def state_text(rows, account_column=False, headers=None):
        if headers is None:
            headers = ["Задача", "Нить", "Его статус"]
            if account_column:
                headers.append("Аккаунт")
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
        for row in rows:
            values = list(row)
            if len(values) < len(headers):
                values.extend([""] * (len(headers) - len(values)))
            lines.append("| " + " | ".join(values) + " |")
        return "\n".join(lines) + "\n"

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def write_quota(self, account, primary_used=0, secondary_used=0, primary_window=10080, secondary_window=10080,
                    primary_reset=None, secondary_reset=None, epoch=None, primary=True, secondary=True, mtime=None):
        snapshot = int(epoch if epoch is not None else self.now - 1)
        primary_reset = primary_reset if primary_reset is not None else snapshot + primary_window * 60
        secondary_reset = secondary_reset if secondary_reset is not None else snapshot + secondary_window * 60
        p = {"used_percent": primary_used, "window_minutes": primary_window, "resets_at": int(primary_reset)} if primary else None
        s = {"used_percent": secondary_used, "window_minutes": secondary_window, "resets_at": int(secondary_reset)} if secondary else None
        session_id = "00000000-0000-4000-8000-00000000" + ("0001" if account == "main" else "0002")
        events = [rate_event(snapshot, p, s)]
        path = self.write_rollout(account, session_id, events, mtime=self.now if mtime is None else mtime)
        return path

    def write_rollout(self, account, session_id, events, parent_id=None, history_start=None, mtime=None):
        home = Path(self.accounts[account]["codex_home"])
        folder = home / "sessions" / "2026" / "11" / "05"
        path = folder / f"rollout-{utc_stamp(self.now).replace(':', '-')}-{session_id}.jsonl"
        records = [task_meta(session_id, parent_id, history_start)] + list(events)
        path.write_text("".join(json_line(row) for row in records), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        self.rollouts[session_id] = path
        return path

    def move_rollout_to_old_named_path(self, session_id):
        home = Path(self.accounts["main"]["codex_home"])
        folder = home / "sessions" / "2026" / "10" / "01"
        folder.mkdir(parents=True, exist_ok=True)
        source = self.rollouts[session_id]
        target = folder / f"rollout-2026-10-01T00-00-00-{session_id}.jsonl"
        source.replace(target)
        os.utime(target, (self.now, self.now))
        self.rollouts[session_id] = target
        return target

    def add_run(self, run_id="run", account="main", session_id="11111111-1111-4111-8111-111111111111",
                age_minutes=0, status=None, live_pid=True, log_session=True, events=None, parent_id=None,
                history_start=None, rollout_mtime=None, pid=42001):
        folder = self.tasks / run_id
        folder.mkdir()
        home = self.accounts[account]["codex_home"]
        (folder / "run.sh").write_text(f"cd -- {self.work}\nexport CODEX_HOME={home}\n", encoding="utf-8")
        start_epoch = self.now - int(age_minutes * 60)
        (folder / "started").write_text(f"{utc_stamp(start_epoch)} pid={pid}\n", encoding="utf-8")
        if status is not None:
            finished_epoch = int(status)
            (folder / "status").write_text(
                "unit=codex-run\nexit=0\nstarted=" + utc_stamp(start_epoch) +
                "\nfinished=" + utc_stamp(finished_epoch) + "\n", encoding="utf-8")
        if log_session and session_id is not None:
            (folder / "reply.txt.run.log").write_text(f"session id: {session_id}\n", encoding="utf-8")
        else:
            (folder / "reply.txt.run.log").write_text("", encoding="utf-8")
        if live_pid:
            (self.proc / str(pid) / "task" / str(pid)).mkdir(parents=True)
            (self.proc / str(pid) / "task" / str(pid) / "children").write_text("", encoding="utf-8")
        if session_id is not None and events is not None:
            self.write_rollout(account, session_id, events, parent_id, history_start, rollout_mtime)
        return folder, start_epoch

    def set_state(self, rows, account_column=False):
        self.state.write_text(self.state_text(rows, account_column), encoding="utf-8")

    def environment(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["TMPDIR"] = str(self.tempdir)
        return env

    def run(self):
        started = time.time()
        result = subprocess.run(
            ["python3", "-B", "-I", str(CHECKER), "--config", str(self.config_path)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30, env=self.environment(),
        )
        finished = time.time()
        return result, started, finished


class LoadCheckAcceptance(unittest.TestCase):
    def fixture(self, name, main_slots=0, acct2_slots=0, alphabetic_path=False):
        return Fixture(name, main_slots, acct2_slots, alphabetic_path)

    def assert_output(self, fixture, result, started, finished, expected, code, ages=(), signal_lines=None,
                      initial_signal="existing signal\n"):
        self.assertEqual(code, result.returncode, result.stderr)
        if code == 2:
            return
        actual = result.stdout.splitlines(keepends=True)
        self.assertEqual(actual, sorted(actual))
        templates = sorted((line + "\n" if not line.endswith("\n") else line) for line in expected)
        self.assertEqual(len(templates), len(actual), result.stdout)
        age_values = iter(ages)
        for wanted, got in zip(templates, actual):
            if AGE not in wanted:
                self.assertEqual(got, wanted)
                continue
            before, after = wanted.split(AGE)
            self.assertTrue(got.startswith(before) and got.endswith(after), (wanted, got))
            value = got[len(before):len(got) - len(after)]
            self.assertRegex(value, r"^\d+\.\d{2}$")
            reference = next(age_values)
            low = Decimal(str((int(started) - reference) / 60)).quantize(Decimal("0.01"))
            high = Decimal(str((finished - reference) / 60)).quantize(Decimal("0.01"))
            number = Decimal(value)
            self.assertGreaterEqual(number, min(low, high))
            self.assertLessEqual(number, max(low, high))
        with self.assertRaises(StopIteration):
            next(age_values)
        self.assertEqual(fixture.report.read_text(encoding="utf-8"), result.stdout)
        current = fixture.signal.read_text(encoding="utf-8")
        if code == 0:
            self.assertEqual(current, initial_signal)
            return
        block = signal_lines if signal_lines is not None else [line for line in result.stdout.splitlines() if line.startswith("SIGNAL ")]
        match = re.fullmatch(re.escape(initial_signal) +
                             r"## LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\n" +
                             re.escape("".join(line + "\n" for line in block)), current)
        self.assertIsNotNone(match, current)
        stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        self.assertGreaterEqual(stamp, int(started))
        self.assertLessEqual(stamp, math.ceil(finished))

    def check(self, fixture, expected=(), code=0, ages=(), diagnostic=(), signal_lines=None,
              initial_signal="existing signal\n"):
        signal_path = Path(fixture.config.get("signal_path", fixture.signal))
        signal_before = self.object_snapshot(signal_path)
        result, started, finished = fixture.run()
        if code == 2:
            self.assertEqual(2, result.returncode, result.stderr)
            for fragment in diagnostic:
                self.assertIn(str(fragment), result.stderr)
            self.assertNotRegex(result.stdout, r"(?m)^(OK|PENDING|SIGNAL)(?:\s|$)")
            self.assertEqual(self.object_snapshot(signal_path), signal_before)
            return result
        self.assert_output(fixture, result, started, finished, expected, code, ages, signal_lines, initial_signal)
        return result

    def object_snapshot(self, path):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None
        if path.is_symlink():
            return ("symlink", os.readlink(path))
        if path.is_dir():
            return ("directory", tuple((child.name, self.object_snapshot(child))
                                        for child in sorted(path.iterdir(), key=lambda item: item.name)))
        if path.is_file():
            return ("file", path.read_bytes())
        return ("other", mode, path.stat().st_size)

    def assert_repeat_output(self, fixture, result, started, finished, expected, initial_signal, first_stamp):
        self.assertEqual(3, result.returncode, result.stderr)
        expected_text = "".join(line + "\n" for line in sorted(expected))
        self.assertEqual(result.stdout, expected_text)
        self.assertEqual(fixture.report.read_text(encoding="utf-8"), expected_text)
        current = fixture.signal.read_text(encoding="utf-8")
        pattern = re.escape(initial_signal) + r"LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ): повтор LOAD-" + re.escape(first_stamp) + r"\n"
        match = re.fullmatch(pattern, current)
        self.assertIsNotNone(match, current)
        stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        self.assertGreaterEqual(stamp, int(started))
        self.assertLessEqual(stamp, math.ceil(finished))
        return current

    def repeat_sequence(self, label):
        f = self.fixture(label)
        session1 = "22222222-2222-4222-8222-222222222222"
        orphan1, _ = f.add_run("orphan-one", "main", session1, age_minutes=1, live_pid=False,
                               events=self.fresh_events(session1, now=f.now), pid=42002)
        line1 = f"SIGNAL 1а main: оборванный прогон; прогон=orphan-one; pid=42002; источник={orphan1 / 'started'}"
        self.check(f, [line1], 3, signal_lines=[line1])
        first_block = f.signal.read_text(encoding="utf-8")
        first_stamp = re.search(r"## LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\n", first_block).group(1)
        time.sleep(1.1)
        before_repeat = f.signal.read_text(encoding="utf-8")
        result, started, finished = f.run()
        self.assert_repeat_output(f, result, started, finished, [line1], before_repeat, first_stamp)
        time.sleep(1.1)
        before_third = f.signal.read_text(encoding="utf-8")
        result, started, finished = f.run()
        self.assert_repeat_output(f, result, started, finished, [line1], before_third, first_stamp)
        time.sleep(1.1)
        session2 = "33333333-3333-4333-8333-333333333333"
        orphan2, _ = f.add_run("orphan-two", "main", session2, age_minutes=1, live_pid=False,
                               events=self.fresh_events(session2, now=f.now), pid=42003)
        line2 = f"SIGNAL 1а main: оборванный прогон; прогон=orphan-two; pid=42003; источник={orphan2 / 'started'}"
        changed = sorted([line1, line2])
        before_changed = f.signal.read_text(encoding="utf-8")
        result, started, finished = f.run()
        self.assert_output(f, result, started, finished, changed, 3, signal_lines=changed,
                           initial_signal=before_changed)

    def idle_line(self, thread_id, rollout, source_line=2, limit="20.00"):
        return f"SIGNAL 2а {thread_id}: простой; без событий={AGE} мин; порог={limit} мин; источник={rollout}:{source_line}"

    def ready_line(self, account, free, ready, fixture):
        return f"SIGNAL 1в {account}: есть работа и места: запусти; свободно={free}; готово={ready}; источник={fixture.state}"

    def split_line(self, account, free, ready, fixture):
        return f"SIGNAL 4а {account}: дроби план; свободно={free}; готово={ready}; источник={fixture.state}"

    def no_snapshot_line(self, account, hours, fixture):
        return f"SIGNAL 1б {account}: нет свежего снимка; предел={hours}.00 ч; источник={Path(fixture.accounts[account]['codex_home']) / 'sessions'}"

    def missing_line(self, identifier, run_id, kind, age=AGE, source=None):
        return f"SIGNAL 5 {identifier}: нет данных; прогон={run_id}; {kind}; возраст={age} мин; источник={source}"

    def pending_line(self, run_id, kind, source):
        return f"PENDING {run_id}: {kind}; источник={source}"

    def fresh_events(self, thread_id, age_minutes=1, now=None):
        return [assistant_event(thread_id, int(now) - int(age_minutes * 60), "Свежая проверка")]

    def healthy_run(self, fixture, run_id="leader", account="main", session_id="11111111-1111-4111-8111-111111111111",
                    age_minutes=0, pid=42001, rollout_mtime=None):
        events = None if session_id in fixture.rollouts else self.fresh_events(session_id, 1, fixture.now)
        return fixture.add_run(run_id, account, session_id, age_minutes, live_pid=True, events=events,
                               rollout_mtime=rollout_mtime, pid=pid)

    def cycle_events(self, thread_id, count=3, start_minutes=10, command="pytest", exit_code=1, output="", now=None):
        now = int(time.time()) if now is None else int(now)
        return [command_event(thread_id, now - (start_minutes - i) * 60, command, exit_code, output,
                              item_id=f"cmd-{i}") for i in range(count)]

    def make_mechanics(self, fixture, own_count, intervals):
        leader = "11111111-1111-4111-8111-111111111111"
        now = fixture.now
        records = []
        helpers = []
        for index, (start_offset, end_offset) in enumerate(intervals, 1):
            helper = f"{index:08d}-3333-4333-8333-{index:012d}"
            helpers.append(helper)
            start = now - start_offset * 60
            end = now - end_offset * 60
            records.append((start, 0, subagent_event(leader, start, "started", helper)))
            records.append((end, 2, subagent_event(leader, end, "completed", helper)))
        command_times = [now - (25 - (4 * i / max(own_count - 1, 1))) * 60 for i in range(own_count)]
        for index, timestamp in enumerate(command_times, 1):
            command_time = int(timestamp)
            call_id = f"leader-call-{index}"
            records.append((command_time, 0, custom_exec_call_event(
                command_time, "leader-turn", call_id,
                f"tools.exec_command({{command: 'work-{index}'}})")))
            command = command_event(leader, command_time, f"work-{index}", 0, "ok\n", f"leader-{index}")
            records.append((command_time, 1, command))
            if index == 1:
                records.append((command_time, 1.5,
                                command_event(leader, command_time, "work-1", 0, "ok\n", "leader-1")))
        records.extend([
            (now - 19 * 60, 0, custom_exec_call_event(now - 19 * 60, "leader-turn", "leader-unpaired-1",
                                                       "tools.exec_command({command: 'unpaired-one'})")),
            (now - 18 * 60, 0, custom_exec_call_event(now - 18 * 60, "leader-turn", "leader-unpaired-2",
                                                       "tools.exec_command({command: 'unpaired-two'})")),
        ])
        records.append((now - 1, 3, assistant_event(leader, now - 1, "Свежий шаг")))
        records.sort(key=lambda item: (item[0], item[1]))
        events = [record for _, _, record in records]
        rollout = fixture.write_rollout("main", leader, events)
        self.healthy_run(fixture, session_id=leader, age_minutes=1)
        for index, helper in enumerate(helpers, 1):
            start_offset, end_offset = intervals[index - 1]
            activity_start = now - start_offset * 60
            activity_end = now - end_offset * 60
            midpoint = (activity_start + activity_end) // 2
            turn_id = f"helper-turn-{index}"
            if len(intervals) == 1:
                child_records = [(activity_start, 0, task_boundary_event("task_started", activity_start, turn_id)),
                                 (activity_start + 60, 1, command_event(helper, activity_start + 60,
                                                                      "helper-work-a", 0, "ok\n", "helper-a")),
                                 (activity_start + 210, 2, container_exec_event(activity_start + 210, "container-one")),
                                 (activity_start + 240, 2, container_exec_event(activity_start + 240, "container-two")),
                                 (activity_start + 300, 1, command_event(helper, activity_start + 300,
                                                                        "helper-work-b", 0, "ok\n", "helper-b")),
                                 (activity_start + 360, 1, command_event(helper, activity_start + 360,
                                                                        "helper-work-c", 0, "ok\n", "helper-c")),
                                 (activity_end, 3, task_boundary_event("task_complete", activity_end, turn_id))]
                child_events = [event for _, _, event in sorted(child_records, key=lambda item: (item[0], item[1]))]
            else:
                child_events = [task_boundary_event("task_started", activity_start, turn_id)]
                if index <= 3:
                    child_events.append(command_event(helper, midpoint, f"helper-work-{index}", 0, "ok\n", f"helper-{index}"))
                else:
                    child_events.append(assistant_event(helper, midpoint, "Помощник работает"))
                if index == 3:
                    child_events.extend([
                        container_exec_event(midpoint + 1, "container-one"),
                        container_exec_event(midpoint + 2, "container-two"),
                    ])
                child_events.append(task_boundary_event("task_complete", activity_end, turn_id))
            fixture.write_rollout("main", helper, child_events, parent_id=leader, history_start=1)
        return rollout, helpers

    def missing_config_key(self, key):
        f = self.fixture("missing-config-" + key.replace(".", "-"))
        cursor = f.config
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        del cursor[parts[-1]]
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path, parts[-1]))

    def test_198_dead_pid_orphan(self):
        f = self.fixture("dead-pid-orphan", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session)
        orphan, _ = f.add_run("orphan", "main", session, live_pid=False, events=None, pid=42002)
        wanted = [f"SIGNAL 1а main: оборванный прогон; прогон=orphan; pid=42002; источник={orphan / 'started'}"]
        self.check(f, wanted, 3)

    def test_198_live_pid_not_orphan(self):
        f = self.fixture("live-pid-not-orphan", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session)
        f.add_run("orphan", "main", session, live_pid=True, events=None, pid=42002)
        self.check(f, ["OK"], 0)

    def test_199_duplicate_uuid_counts_once(self):
        f = self.fixture("duplicate-uuid-counts-once", main_slots=2)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session)
        f.add_run("second-copy", "main", session, live_pid=True, events=None, pid=42002)
        self.check(f, [self.split_line("main", 1, 0, f)], 3)

    def test_199_distinct_uuid_counts_twice(self):
        f = self.fixture("distinct-uuid-counts-twice", main_slots=2)
        self.healthy_run(f)
        session = "22222222-2222-4222-8222-222222222222"
        f.add_run("second", "main", session, live_pid=True, events=self.fresh_events(session, now=f.now), pid=42002)
        self.check(f, ["OK"], 0)

    def test_200_gap_11_signals(self):
        f = self.fixture("gap-11")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=39, primary_window=300, primary_reset=snapshot + 150 * 60,
                      secondary_used=0, secondary_window=10080)
        source = f.rollouts[next(key for key in f.rollouts if key.endswith("0001"))]
        self.check(f, [f"SIGNAL 1б main: primary: расход=39.00%; ожидается=50.00%; отставание=11.00 п.п.; источник={source}:2"], 3)

    def test_200_gap_exactly_10_does_not_signal(self):
        f = self.fixture("gap-10")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=40, primary_window=300, primary_reset=snapshot + 150 * 60,
                      secondary_used=0, secondary_window=10080)
        self.check(f, ["OK"], 0)

    def test_200_gap_5_does_not_signal(self):
        f = self.fixture("gap-5")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=45, primary_window=300, primary_reset=snapshot + 150 * 60,
                      secondary_used=0, secondary_window=10080)
        self.check(f, ["OK"], 0)

    def test_201_secondary_window_is_independent(self):
        f = self.fixture("secondary-window")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=0, primary_window=10080, primary_reset=snapshot + 10080 * 60,
                      secondary_used=39, secondary_window=300, secondary_reset=snapshot + 150 * 60)
        source = f.rollouts[next(key for key in f.rollouts if key.endswith("0001"))]
        self.check(f, [f"SIGNAL 1б main: secondary: расход=39.00%; ожидается=50.00%; отставание=11.00 п.п.; источник={source}:2"], 3)

    def test_201_null_secondary_is_ignored(self):
        f = self.fixture("null-secondary")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=20, primary_window=10080, primary_reset=snapshot + 6 * 24 * 3600,
                      secondary=False)
        self.check(f, ["OK"], 0)

    def test_202_all_windows_expired(self):
        f = self.fixture("all-windows-expired")
        now = f.now
        f.write_quota("main", primary_used=20, primary_window=300, primary_reset=now - 60,
                      secondary_used=20, secondary_window=10080, secondary_reset=now - 60)
        self.check(f, [self.no_snapshot_line("main", 6, f)], 3)

    def test_202_one_expired_one_live(self):
        f = self.fixture("one-expired-one-live")
        now = f.now - 1
        f.write_quota("main", primary_used=20, primary_window=300, primary_reset=now - 60,
                      secondary_used=0, secondary_window=10080, secondary_reset=now + 10080 * 60)
        self.check(f, ["OK"], 0)

    def test_202_no_runs_no_fresh_snapshot_immediate_1b(self):
        f = self.fixture("no-snapshot-no-runs")
        f.write_quota("main", primary=False, secondary=False)
        self.check(f, [self.no_snapshot_line("main", 6, f)], 3)

    def test_203_snapshot_age_5h58(self):
        f = self.fixture("snapshot-age-558")
        old = f.now - 5 * 3600 - 58 * 60
        f.write_quota("main", epoch=old, primary_used=0, primary_window=10080, primary_reset=old + 10080 * 60)
        self.check(f, ["OK"], 0)

    def test_203_snapshot_age_6h02(self):
        f = self.fixture("snapshot-age-602")
        old = f.now - 6 * 3600 - 2 * 60
        f.write_quota("main", epoch=old, primary_used=0, primary_window=10080, primary_reset=old + 10080 * 60)
        self.check(f, [self.no_snapshot_line("main", 6, f)], 3)

    def test_204_early_plan_deadline_shortens_window(self):
        f = self.fixture("early-deadline")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=20, primary_window=10080,
                      primary_reset=snapshot + 6 * 24 * 3600, secondary=False)
        f.config["accounts"]["main"]["plan_deadline"] = snapshot + 24 * 3600
        f.write_config()
        source = f.rollouts[next(key for key in f.rollouts if key.endswith("0001"))]
        self.check(f, [f"SIGNAL 1б main: primary: расход=20.00%; ожидается=50.00%; отставание=30.00 п.п.; источник={source}:2"], 3)

    def test_204_null_deadline_uses_reset(self):
        f = self.fixture("null-deadline")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=20, primary_window=10080,
                      primary_reset=snapshot + 6 * 24 * 3600, secondary=False)
        self.check(f, ["OK"], 0)

    def test_205_free_slot_with_ready_work(self):
        f = self.fixture("free-slot-ready", main_slots=4)
        self.healthy_run(f)
        f.set_state([("task-1", "", "Готова к запуску"), ("task-2", "", "Готова к запуску"),
                     ("task-3", "", "Готова к запуску")])
        self.check(f, [self.ready_line("main", 3, 3, f)], 3)

    def test_205_all_slots_occupied(self):
        f = self.fixture("all-slots-occupied", main_slots=4)
        self.healthy_run(f)
        for i in range(2, 5):
            session = f"{i:08d}-2222-4222-8222-{i:012d}"
            f.add_run(f"run-{i}", "main", session, live_pid=True,
                      events=self.fresh_events(session, now=f.now), pid=42000 + i)
        f.set_state([("task-1", "", "Готова к запуску"), ("task-2", "", "Готова к запуску"),
                     ("task-3", "", "Готова к запуску")])
        self.check(f, ["OK"], 0)

    def test_205_free_slot_without_ready_work(self):
        f = self.fixture("free-slot-no-ready", main_slots=4)
        self.healthy_run(f)
        self.check(f, [self.split_line("main", 3, 0, f)], 3)

    def test_206_idle_age_18m(self):
        f = self.fixture("idle-18", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 18 * 60
        rollout = f.write_rollout("main", session, [assistant_event(session, event_time, "Работа продолжается")])
        self.healthy_run(f, session_id=session, age_minutes=1)
        self.check(f, ["OK"], 0)

    def test_206_idle_age_22m(self):
        f = self.fixture("idle-22", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 22 * 60
        rollout = f.write_rollout("main", session, [assistant_event(session, event_time, "Работа продолжается")])
        self.healthy_run(f, session_id=session, age_minutes=1)
        self.check(f, [self.idle_line(session, rollout)], 3, ages=(event_time,))

    def test_207_uuid_journal_read_despite_old_mtime(self):
        f = self.fixture("uuid-journal-old-mtime", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 40 * 60
        rollout = f.write_rollout("main", session, [command_event(session, event_time, "pytest", 1, "", "finished")],
                                  mtime=f.now - 40 * 60)
        self.healthy_run(f, session_id=session, age_minutes=1)
        self.check(f, [self.idle_line(session, rollout)], 3, ages=(event_time,))

    def test_207_pending_command_and_live_child_exempt_idle(self):
        f = self.fixture("pending-command-child", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        running = command_event(session, now - 40 * 60, "pytest", status="running", item_id="running")
        rollout = f.write_rollout("main", session, [running], mtime=now - 40 * 60)
        folder, _ = self.healthy_run(f, session_id=session, age_minutes=40)
        pid = int((folder / "started").read_text(encoding="utf-8").split("pid=")[1])
        child_file = f.proc / str(pid) / "task" / str(pid) / "children"
        child_file.write_text("43001\n", encoding="utf-8")
        (f.proc / "43001" / "task" / "43001").mkdir(parents=True)
        (f.proc / "43001" / "task" / "43001" / "children").write_text("", encoding="utf-8")
        self.check(f, ["OK"], 0)

    def test_208_pending_command_without_child_still_idle(self):
        f = self.fixture("pending-command-no-child", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 22 * 60
        rollout = f.write_rollout("main", session,
                                  [command_event(session, event_time, "pytest", status="running", item_id="running")])
        self.healthy_run(f, session_id=session, age_minutes=22)
        self.check(f, [self.idle_line(session, rollout)], 3, ages=(event_time,))

    def test_208_child_without_pending_command_still_idle(self):
        f = self.fixture("child-no-pending-command", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 22 * 60
        rollout = f.write_rollout("main", session, [assistant_event(session, event_time, "Завершено")])
        folder, _ = self.healthy_run(f, session_id=session, age_minutes=22)
        pid = int((folder / "started").read_text(encoding="utf-8").split("pid=")[1])
        child_file = f.proc / str(pid) / "task" / str(pid) / "children"
        child_file.write_text("43002\n", encoding="utf-8")
        (f.proc / "43002" / "task" / "43002").mkdir(parents=True)
        (f.proc / "43002" / "task" / "43002" / "children").write_text("", encoding="utf-8")
        self.check(f, [self.idle_line(session, rollout)], 3, ages=(event_time,))

    def test_209_running_helper_idle(self):
        f = self.fixture("running-helper-idle", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 22 * 60, "started", helper),
                         assistant_event(leader, now - 60, "Руководитель продолжает работу")]
        self.healthy_run(f, session_id=leader)
        f.write_rollout("main", leader, parent_events)
        event_time = now - 22 * 60
        task_event = task_boundary_event("task_started", event_time, "turn-helper")
        helper_rollout = f.write_rollout("main", helper, [task_event, assistant_event(helper, event_time, "Помощник работает")],
                                         parent_id=leader, history_start=1)
        self.check(f, [self.idle_line(helper, helper_rollout, source_line=3)], 3, ages=(event_time,))

    def test_209_fresh_or_closed_helper_not_idle(self):
        f = self.fixture("helpers-fresh-closed", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        fresh = "33333333-3333-4333-8333-333333333333"
        closed = "44444444-4444-4444-8444-444444444444"
        now = f.now
        parent = [subagent_event(leader, now - 90, "started", fresh),
                  subagent_event(leader, now - 30 * 60, "started", closed),
                  subagent_event(leader, now - 25 * 60, "completed", closed),
                  assistant_event(leader, now - 1, "Свежий шаг руководителя")]
        self.healthy_run(f, session_id=leader)
        f.write_rollout("main", leader, parent)
        f.write_rollout("main", fresh,
                        [task_boundary_event("task_started", now - 60, f"turn-{fresh}"),
                         assistant_event(fresh, now - 30, "Работа")],
                        parent_id=leader, history_start=1)
        f.write_rollout("main", closed,
                        [task_boundary_event("task_started", now - 30 * 60, f"turn-{closed}"),
                         assistant_event(closed, now - 25 * 60, "Закрытая работа"),
                         task_boundary_event("task_complete", now - 25 * 60, f"turn-{closed}")],
                        parent_id=leader, history_start=1)
        self.check(f, ["OK"], 0)

    def test_209_parent_pending_command_does_not_exempt_helper(self):
        f = self.fixture("parent-pending-helper-idle", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent = [command_event(leader, now - 22 * 60, "pytest", status="running", item_id="parent-running"),
                  subagent_event(leader, now - 22 * 60, "started", helper),
                  assistant_event(leader, now - 60, "Руководитель продолжает работу")]
        folder, _ = self.healthy_run(f, session_id=leader, age_minutes=22)
        pid = int((folder / "started").read_text(encoding="utf-8").split("pid=")[1])
        child_file = f.proc / str(pid) / "task" / str(pid) / "children"
        child_file.write_text("43010\n", encoding="utf-8")
        (f.proc / "43010" / "task" / "43010").mkdir(parents=True)
        (f.proc / "43010" / "task" / "43010" / "children").write_text("", encoding="utf-8")
        f.write_rollout("main", leader, parent)
        event_time = now - 22 * 60
        helper_rollout = f.write_rollout("main", helper,
                                         [task_boundary_event("task_started", event_time, "helper-turn"),
                                          assistant_event(helper, event_time, "Помощник")],
                                         parent_id=leader, history_start=1)
        self.check(f, [self.idle_line(helper, helper_rollout, source_line=3)], 3, ages=(event_time,))

    def test_210_waiting_age_13m(self):
        f = self.fixture("waiting-13")
        session = "11111111-1111-4111-8111-111111111111"
        finished = f.now - 13 * 60
        run, _ = f.add_run("finished", "main", session, age_minutes=25, status=finished,
                           live_pid=False, events=[assistant_event(session, finished - 1, "Завершено")])
        f.set_state([("task", session, "В работе")])
        self.check(f, ["OK"], 0)

    def test_210_waiting_age_17m(self):
        f = self.fixture("waiting-17")
        session = "11111111-1111-4111-8111-111111111111"
        finished = f.now - 17 * 60
        run, _ = f.add_run("finished", "main", session, age_minutes=30, status=finished,
                           live_pid=False, events=[assistant_event(session, finished - 1, "Завершено")])
        f.set_state([("task", session, "В работе")])
        status = run / "status"
        self.check(f, [f"SIGNAL 2б {session}: ожидание; после finished={AGE} мин; порог=15.00 мин; источник={status}"],
                   3, ages=(finished,))

    def test_210_newer_run_supersedes_old(self):
        f = self.fixture("newer-run-supersedes")
        session = "11111111-1111-4111-8111-111111111111"
        old_finished = f.now - 30 * 60
        f.add_run("old", "main", session, age_minutes=60, status=old_finished, live_pid=False,
                  events=[assistant_event(session, old_finished - 1, "Завершено")])
        new_event = f.now - 60
        f.add_run("new", "main", session, age_minutes=1, live_pid=True, events=None, pid=42002)
        f.write_rollout("main", session, [assistant_event(session, old_finished - 1, "Завершено"),
                                            assistant_event(session, new_event, "Новый прогон")])
        f.set_state([("task", session, "В работе")])
        self.check(f, ["OK"], 0)

    def test_210_each_closed_prefix(self):
        f = self.fixture("closed-prefixes")
        now = f.now
        rows = []
        for index, prefix in enumerate(("Принята задача", "Сохранена задача", "Закрыта задача"), 1):
            session = f"{index:08d}-2222-4222-8222-{index:012d}"
            finished = now - 17 * 60
            run_id = f"closed-{index}"
            run, _ = f.add_run(run_id, "main", session, age_minutes=30, status=finished,
                               live_pid=False, events=[assistant_event(session, finished - 1, "Завершено")])
            rows.append((run_id, session, prefix))
        f.set_state(rows)
        self.check(f, ["OK"], 0)

    def test_211_three_same_completed_attempts_signal(self):
        f = self.fixture("three-cycle", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        events = self.cycle_events(session, count=3, now=f.now)
        rollout = f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_211_two_attempts_do_not_signal(self):
        f = self.fixture("two-cycle-attempts", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        f.write_rollout("main", session, self.cycle_events(session, count=2, now=f.now))
        self.healthy_run(f, session_id=session)
        self.check(f, ["OK"], 0)

    def test_211_exit_and_sha_are_part_of_key(self):
        f = self.fixture("cycle-distinct-results", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "a"),
                  command_event(session, now - 9 * 60, "pytest", 0, "", "b"),
                  command_event(session, now - 8 * 60, "pytest", 1, "x\n", "c")]
        f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        self.check(f, ["OK"], 0)

    def test_211_incomplete_command_is_not_attempt(self):
        f = self.fixture("incomplete-command-cycle", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        events = self.cycle_events(session, count=2, now=f.now)
        events.append(command_event(session, f.now - 8 * 60, "pytest", status="running", item_id="running"))
        f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        self.check(f, ["OK"], 0)

    def test_cycle_output_newline_is_significant(self):
        f = self.fixture("cycle-output-newline", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "pytest", 1, "x\n", "a"),
                  command_event(session, now - 9 * 60, "pytest", 1, "x\n", "b"),
                  command_event(session, now - 8 * 60, "pytest", 1, "x", "c")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_cycle_quoted_hash_is_significant(self):
        f = self.fixture("cycle-quoted-hash", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "test '#b' = z", 1, "", "a"),
                  command_event(session, now - 9 * 60, "test '#b' = z", 1, "", "b"),
                  command_event(session, now - 8 * 60, "test '#c' = z", 1, "", "c")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_cycle_nonword_hash_is_significant(self):
        f = self.fixture("cycle-nonword-hash", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "test a#b = z", 1, "", "a"),
                  command_event(session, now - 9 * 60, "test a#b = z", 1, "", "b"),
                  command_event(session, now - 8 * 60, "test a#c = z", 1, "", "c")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_212_shell_comment_and_outer_space_normalize(self):
        f = self.fixture("cycle-comment-normalization", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        commands = ["pytest # один", " pytest  # два", "pytest"]
        events = [command_event(session, now - (10 - i) * 60, cmd, 1, "", f"cmd-{i}")
                  for i, cmd in enumerate(commands)]
        rollout = f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_212_quoted_spaces_remain_significant(self):
        f = self.fixture("cycle-quoted-space", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, 'pytest "a b"', 1, "", "a"),
                  command_event(session, now - 9 * 60, 'pytest "a  b"', 1, "", "b"),
                  command_event(session, now - 8 * 60, 'pytest "a   b"', 1, "", "c")]
        f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        self.check(f, ["OK"], 0)

    def test_cycle_multiline_action_is_escaped(self):
        f = self.fixture("cycle-multiline-action", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        command = "test x = y\ntest a = b"
        f.write_rollout("main", session, self.cycle_events(session, count=3, command=command, now=f.now))
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=test x = y\\ntest a = b; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_cycle_tab_and_cr_in_quoted_action_are_escaped(self):
        f = self.fixture("cycle-tab-cr-action", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        command = "test 'a\tb\rc' = z"
        f.write_rollout("main", session, self.cycle_events(session, count=3, command=command, now=f.now))
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=test 'a\\tb\\rc' = z; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_212_intervening_command_does_not_break_series(self):
        f = self.fixture("cycle-intervening-command", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "a"),
                  command_event(session, now - 9 * 60, "git status", 0, "ok\n", "b"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "c"),
                  command_event(session, now - 7 * 60, "pytest", 1, "", "d")]
        rollout = f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:5"
        self.check(f, [line], 3)

    def test_cycle_four_attempts_emit_one_group_signal(self):
        f = self.fixture("four-cycle-attempts", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        events = self.cycle_events(session, count=4, now=f.now)
        rollout = f.write_rollout("main", session, events)
        self.healthy_run(f, session_id=session)
        line = f"SIGNAL 3а {session}: цикл; попыток=4; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:5"
        self.check(f, [line], 3)

    def test_214_helper_own_history_is_counted(self):
        f = self.fixture("helper-cycle", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_event = [subagent_event(leader, now - 11 * 60, "started", helper),
                        assistant_event(leader, now - 60, "Руководитель продолжает")]
        self.healthy_run(f, session_id=leader)
        f.write_rollout("main", leader, parent_event)
        events = [task_boundary_event("task_started", now - 11 * 60, "helper-turn")]
        events.extend(self.cycle_events(helper, count=3, now=now))
        rollout = f.write_rollout("main", helper, events, parent_id=leader, history_start=1)
        line = f"SIGNAL 3а {helper}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:5"
        self.check(f, [line], 3)

    def test_214_inherited_prefix_is_not_helper_history(self):
        f = self.fixture("helper-inherited-prefix", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        self.healthy_run(f, session_id=leader)
        f.write_rollout("main", leader, [subagent_event(leader, now - 60, "started", helper),
                                          assistant_event(leader, now - 1, "Руководитель продолжает")])
        events = [command_event(leader, now - 10 * 60, "pytest", 1, "", "legacy-1"),
                  command_event(leader, now - 9 * 60, "pytest", 1, "", "legacy-2"),
                  command_event(leader, now - 8 * 60, "pytest", 1, "", "legacy-3")]
        events.extend([task_boundary_event("task_started", now - 60, "helper-own-turn"),
                       assistant_event(helper, now - 30, "Собственный ход помощника")])
        f.write_rollout("main", helper, events, parent_id=leader, history_start=4)
        self.check(f, ["OK"], 0)

    def test_215_active_age_118m_no_stagnation(self):
        f = self.fixture("stagnation-age-118", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        f.write_rollout("main", session, [assistant_event(session, now - 60, "Активная работа")])
        _, start_time = f.add_run("leader", "main", session, age_minutes=118, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_215_active_age_122m_without_new_step_signals(self):
        f = self.fixture("stagnation-age-122", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        f.write_rollout("main", session, [assistant_event(session, now - 60, "Работа без строки хода")])
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_215_new_unique_step_prevents_stagnation(self):
        f = self.fixture("stagnation-new-step", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        f.write_rollout("main", session, [assistant_event(session, now - 10 * 60, "ХОД: [x] Новый шаг завершён")])
        f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_216_repeated_old_step_is_not_progress(self):
        f = self.fixture("stagnation-repeated-step", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        text = "ХОД: [x] Повторённый старый шаг"
        f.write_rollout("main", session, [assistant_event(session, now - 180 * 60, text),
                                           assistant_event(session, now - 10 * 60, text)])
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_216_new_unique_assistant_step_is_progress(self):
        f = self.fixture("stagnation-unique-step", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        f.write_rollout("main", session, [assistant_event(session, now - 180 * 60, "ХОД: [x] Старый шаг"),
                                           assistant_event(session, now - 10 * 60, "ХОД: [x] Новый шаг")])
        f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_216_unique_historical_step_in_old_mtime_file_prevents_stagnation(self):
        f = self.fixture("stagnation-old-mtime-step", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [assistant_event(session, now - 60 * 60, "ХОД: [x] Исторический новый шаг"),
                  command_event(session, now - 40 * 60, "pytest", status="running", item_id="running")]
        rollout = f.write_rollout("main", session, events, mtime=now - 40 * 60)
        folder, _ = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        pid = int((folder / "started").read_text(encoding="utf-8").split("pid=")[1])
        children = f.proc / str(pid) / "task" / str(pid) / "children"
        children.write_text("43003\n", encoding="utf-8")
        (f.proc / "43003" / "task" / "43003").mkdir(parents=True)
        (f.proc / "43003" / "task" / "43003" / "children").write_text("", encoding="utf-8")
        self.check(f, ["OK"], 0)

    def test_217_contiguous_resume_intervals_reach_horizon(self):
        f = self.fixture("resume-overlap", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        _, chain_start = f.add_run("resume-old", "main", session, age_minutes=122, status=now - 60 * 60,
                                   live_pid=False, events=None)
        f.add_run("resume-current", "main", session, age_minutes=61, live_pid=True, events=None, pid=42002)
        rollout = f.write_rollout("main", session, [assistant_event(session, now - 60, "Нет новой строки хода")])
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(chain_start,))

    def test_217_positive_resume_gap_resets_horizon(self):
        f = self.fixture("resume-gap", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        f.add_run("resume-old", "main", session, age_minutes=140, status=now - 118 * 60 - 1,
                  live_pid=False, events=None)
        f.add_run("resume-current", "main", session, age_minutes=118, live_pid=True, events=None, pid=42002)
        f.write_rollout("main", session, [assistant_event(session, now - 60, "Нет новой строки хода")])
        self.check(f, ["OK"], 0)

    def test_221_free8_ready1_signals_1v_and_4a(self):
        f = self.fixture("free8-ready1", main_slots=8)
        f.set_state([("task-1", "", "Готова к запуску")])
        self.check(f, [self.ready_line("main", 8, 1, f), self.split_line("main", 8, 1, f)], 3)

    def test_221_free8_ready8_signals_1v_without_4a(self):
        f = self.fixture("free8-ready8", main_slots=8)
        f.set_state([(f"task-{i}", "", "Готова к запуску") for i in range(1, 9)])
        self.check(f, [self.ready_line("main", 8, 8, f)], 3)

    def test_222_one_ready_row_is_insufficient_for_eight_slots(self):
        f = self.fixture("one-ready-row-eight", main_slots=8)
        f.set_state([("task-1", "", "Готова к запуску; также task-2 task-3 task-4 task-5 task-6 task-7 task-8")])
        self.check(f, [self.ready_line("main", 8, 1, f), self.split_line("main", 8, 1, f)], 3)

    def test_222_seven_separate_ready_tracks_complete_eight(self):
        f = self.fixture("seven-ready-tracks", main_slots=8)
        f.set_state([(f"task-{i}", "", "Готова к запуску") for i in range(1, 9)])
        self.check(f, [self.ready_line("main", 8, 8, f)], 3)

    def test_223_account_column_filters_configured_mode(self):
        f = self.fixture("account-column-filter", main_slots=1, acct2_slots=1)
        self.healthy_run(f, account="main")
        f.config["plan"]["columns"]["account"] = "Аккаунт"
        f.write_config()
        f.set_state([("task", "", "Готова к запуску", "main")], account_column=True)
        self.check(f, [self.split_line("acct2", 1, 0, f)], 3)

    def test_223_no_account_column_uses_global_count(self):
        f = self.fixture("global-ready-count", main_slots=1, acct2_slots=1)
        self.healthy_run(f, account="main")
        f.set_state([("task", "", "Готова к запуску")])
        self.check(f, [self.ready_line("acct2", 1, 1, f)], 3)

    def test_223_empty_and_multi_account_cells(self):
        f = self.fixture("ready-shared-account-cells", main_slots=2, acct2_slots=2)
        f.config["plan"]["columns"]["account"] = "Аккаунт"
        f.write_config()
        f.set_state([("task-empty", "", "Готова к запуску", ""),
                     ("task-both", "", "Готова к запуску", "main,acct2")], account_column=True)
        self.check(f, [self.ready_line("acct2", 2, 2, f), self.ready_line("main", 2, 2, f)], 3)

    def test_224_21_leader_commands_and_59_helpers_signal(self):
        f = self.fixture("mechanics-21-59", main_slots=1)
        intervals = [(10, 9)] * 59
        rollout, _ = self.make_mechanics(f, 21, intervals)
        line = f"SIGNAL 4б 11111111-1111-4111-8111-111111111111: дроби; команд=21; порог=20; максимум помощников на интервале=59; предел=60; источник={rollout}:44"
        self.check(f, [line], 3)

    def test_224_20_leader_commands_do_not_signal(self):
        f = self.fixture("mechanics-20-59", main_slots=1)
        rollout, _ = self.make_mechanics(f, 20, [(10, 9)] * 59)
        self.check(f, ["OK"], 0)

    def test_224_helper_limit_reached_in_window_suppresses_signal(self):
        f = self.fixture("mechanics-21-60", main_slots=1)
        rollout, _ = self.make_mechanics(f, 21, [(10, 9)] * 60)
        self.check(f, ["OK"], 0)

    def test_224_many_distinct_helpers_below_simultaneous_limit(self):
        f = self.fixture("mechanics-70-helpers", main_slots=1)
        intervals = [(27, 23)] * 35 + [(22, 20)] * 35
        rollout, _ = self.make_mechanics(f, 21, intervals)
        line = f"SIGNAL 4б 11111111-1111-4111-8111-111111111111: дроби; команд=21; порог=20; максимум помощников на интервале=35; предел=60; источник={rollout}:149"
        self.check(f, [line], 3)

    def test_225_helper_peak_outside_command_span_suppresses_signal(self):
        f = self.fixture("mechanics-peak-outside", main_slots=1)
        rollout, _ = self.make_mechanics(f, 21, [(12, 5)] * 60)
        self.check(f, ["OK"], 0)

    def test_225_completed_helper_history_in_window_counts(self):
        f = self.fixture("mechanics-completed-history", main_slots=1)
        rollout, _ = self.make_mechanics(f, 21, [(27, 20)] * 60)
        self.check(f, ["OK"], 0)

    def test_226_only_leader_commands_count(self):
        f = self.fixture("mechanics-only-leader-counts", main_slots=1)
        rollout, _ = self.make_mechanics(f, 21, [(27, 20)])
        line = f"SIGNAL 4б 11111111-1111-4111-8111-111111111111: дроби; команд=21; порог=20; максимум помощников на интервале=1; предел=60; источник={rollout}:45"
        self.check(f, [line], 3)

    def test_226_helpers_own_commands_do_not_become_leader_commands(self):
        f = self.fixture("mechanics-helper-commands", main_slots=1)
        rollout, helpers = self.make_mechanics(f, 20, [(27, 20)])
        helper = helpers[0]
        now = f.now
        child_events = [task_boundary_event("task_started", now - 27 * 60, "helper-turn")]
        child_events.extend(command_event(helper, now - (25 - i / 10) * 60, f"helper-task-{i}", 0, "ok\n", f"helper-task-{i}")
                            for i in range(21))
        child_events.extend([container_exec_event(now - 20 * 60 + 1, "container-one"),
                             container_exec_event(now - 20 * 60 + 2, "container-two"),
                             task_boundary_event("task_complete", now - 19 * 60, "helper-turn")])
        f.write_rollout("main", helper, child_events, parent_id="11111111-1111-4111-8111-111111111111", history_start=1)
        self.check(f, ["OK"], 0)

    def test_227_session_id_missing_age_3m_pending(self):
        f = self.fixture("missing-session-id-3m", main_slots=1)
        run, start_time = f.add_run("run", "main", None, age_minutes=3, live_pid=True, log_session=False, events=None)
        source = run / "reply.txt.run.log"
        self.check(f, [self.pending_line("run", "session id", source)], 0)

    def test_227_session_id_missing_age_7m_signals(self):
        f = self.fixture("missing-session-id-7m", main_slots=1)
        run, start_time = f.add_run("run", "main", None, age_minutes=7, live_pid=True, log_session=False, events=None)
        source = run / "reply.txt.run.log"
        self.check(f, [self.missing_line("main", "run", "session id", source=source)], 3, ages=(start_time,))

    def test_227_unknown_session_id_blocks_dependent_1v_and_4a(self):
        f = self.fixture("missing-session-id-blocks-capacity", main_slots=8)
        run, _ = f.add_run("run", "main", None, age_minutes=3, live_pid=True, log_session=False, events=None)
        f.set_state([("task", "", "Готова к запуску")])
        self.check(f, [self.pending_line("run", "session id", run / "reply.txt.run.log")], 0)

    def test_228_rollout_missing_age_7m_signals(self):
        f = self.fixture("missing-rollout-7m", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        run, start_time = f.add_run("run", "main", session, age_minutes=7, live_pid=True, events=None)
        source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"
        line = self.missing_line(session, "run", "журнал", source=source)
        self.check(f, [line], 3, ages=(start_time,))

    def test_228_present_rollout_has_no_missing_data_signal(self):
        f = self.fixture("present-rollout", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session, age_minutes=7)
        self.check(f, ["OK"], 0)

    def test_229_missing_rate_limits_age_3m_is_pending_not_1b(self):
        f = self.fixture("missing-rate-limits-3m", main_slots=1)
        f.write_quota("main", primary=False, secondary=False)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session, age_minutes=3)
        source = Path(f.accounts["main"]["codex_home"]) / "sessions"
        self.check(f, [self.pending_line("leader", "rate_limits", source)], 0)

    def test_229_missing_rate_limits_age_7m_signals_data_gap(self):
        f = self.fixture("missing-rate-limits-7m", main_slots=1)
        f.write_quota("main", primary=False, secondary=False)
        session = "11111111-1111-4111-8111-111111111111"
        run, start_time = self.healthy_run(f, session_id=session, age_minutes=7)
        source = Path(f.accounts["main"]["codex_home"]) / "sessions"
        lines = [self.no_snapshot_line("main", 6, f), self.missing_line(session, "leader", "rate_limits", source=source)]
        self.check(f, lines, 3, ages=(start_time,))

    def test_230_missing_plan_age_7m_signals(self):
        f = self.fixture("missing-plan-7m", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        run, start_time = self.healthy_run(f, session_id=session, age_minutes=7)
        f.state.unlink()
        line = self.missing_line(session, "leader", "источник плана", source=f.state)
        self.check(f, [line], 3, ages=(start_time,))

    def test_230_existing_empty_plan_is_ready_zero(self):
        f = self.fixture("empty-plan-ready-zero", main_slots=1)
        self.healthy_run(f)
        f.set_state([])
        self.check(f, ["OK"], 0)

    def test_231_pending_without_signal_has_code0_and_no_ok(self):
        f = self.fixture("pending-only", main_slots=1)
        run, _ = f.add_run("run", "main", None, age_minutes=3, live_pid=True, log_session=False, events=None)
        self.check(f, [self.pending_line("run", "session id", run / "reply.txt.run.log")], 0)

    def test_232_pending_and_orphan_signal_are_both_reported(self):
        f = self.fixture("pending-and-orphan", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        pending, _ = f.add_run("pending", "main", session, age_minutes=3, live_pid=True, events=None)
        orphan_id = "22222222-2222-4222-8222-222222222222"
        orphan, _ = f.add_run("orphan", "main", orphan_id,
                              age_minutes=3, live_pid=False, events=self.fresh_events(orphan_id, now=f.now), pid=42002)
        pending_source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"
        orphan_line = f"SIGNAL 1а main: оборванный прогон; прогон=orphan; pid=42002; источник={orphan / 'started'}"
        self.check(f, [self.pending_line("pending", "журнал", pending_source), orphan_line], 3,
                   signal_lines=[orphan_line])

    def test_232_pending_alone_after_orphan_removal_does_not_append_load(self):
        f = self.fixture("pending-after-orphan", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        pending, _ = f.add_run("pending", "main", session, age_minutes=3, live_pid=True, events=None)
        orphan_id = "22222222-2222-4222-8222-222222222222"
        orphan, _ = f.add_run("orphan", "main", orphan_id,
                              age_minutes=3, live_pid=False, events=self.fresh_events(orphan_id, now=f.now), pid=42002)
        pending_source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"
        orphan_line = f"SIGNAL 1а main: оборванный прогон; прогон=orphan; pid=42002; источник={orphan / 'started'}"
        self.check(f, [self.pending_line("pending", "журнал", pending_source), orphan_line], 3,
                   signal_lines=[orphan_line])
        first_signal = f.signal.read_text(encoding="utf-8")
        now = f.now
        (orphan / "status").write_text(f"unit=codex-run\nexit=0\nstarted={utc_stamp(now - 180)}\nfinished={utc_stamp(now)}\n",
                                       encoding="utf-8")
        result, started, finished = f.run()
        self.assertEqual(0, result.returncode, result.stderr)
        expected = self.pending_line("pending", "журнал", pending_source) + "\n"
        self.assertEqual(result.stdout, expected)
        self.assertEqual(f.report.read_text(encoding="utf-8"), expected)
        self.assertEqual(f.signal.read_text(encoding="utf-8"), first_signal)

    def test_233_missing_source_age_3m_is_pending(self):
        f = self.fixture("missing-plan-3m", main_slots=1)
        run, _ = self.healthy_run(f, age_minutes=3)
        f.state.unlink()
        self.check(f, [self.pending_line("leader", "источник плана", f.state)], 0)

    def test_233_missing_source_age_7m_is_signal5(self):
        f = self.fixture("missing-plan-7m-signal5", main_slots=1)
        run, start_time = self.healthy_run(f, age_minutes=7)
        f.state.unlink()
        self.check(f, [self.missing_line("11111111-1111-4111-8111-111111111111", "leader", "источник плана", source=f.state)], 3,
                   ages=(start_time,))

    def test_234_complete_healthy_fixture_is_exact_ok(self):
        f = self.fixture("healthy-all-slots", main_slots=8, acct2_slots=4)
        for account, count, offset in (("main", 8, 1), ("acct2", 4, 20)):
            for index in range(count):
                session = f"{offset + index:08d}-2222-4222-8222-{offset + index:012d}"
                f.add_run(f"{account}-{index}", account, session, age_minutes=1, live_pid=True,
                          events=self.fresh_events(session, now=f.now), pid=43000 + offset + index)
        f.set_state([("one-ready-task", "", "Готова к запуску")])
        self.check(f, ["OK"], 0)

    def test_235_missing_config_key_reports_key_and_path(self):
        self.missing_config_key("accounts.main.slots")

    def test_235_wrong_config_type_reports_path(self):
        f = self.fixture("wrong-config-type")
        f.config["thresholds"]["idle_minutes"] = "20"
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_235_missing_config_path_is_code2(self):
        f = self.fixture("missing-config-path")
        absent = f.root / "absent-config.json"
        result = subprocess.run(["python3", "-B", "-I", str(CHECKER), "--config", str(absent)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env=f.environment())
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn(str(absent), result.stderr)
        self.assertNotRegex(result.stdout, r"(?m)^(OK|PENDING|SIGNAL)(?:\s|$)")
        self.assertEqual(f.signal.read_text(encoding="utf-8"), "existing signal\n")

    def test_235_invalid_report_path_is_code2(self):
        f = self.fixture("invalid-report-path")
        f.config["report_path"] = "relative-report.txt"
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_235_missing_observation_in_grace_is_not_code2(self):
        f = self.fixture("missing-observation-grace", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        run, _ = f.add_run("run", "main", session, age_minutes=3, live_pid=True, events=None)
        source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"
        self.check(f, [self.pending_line("run", "журнал", source)], 0)

    def test_config_missing_tasks_dir(self):
        self.missing_config_key("tasks_dir")

    def test_config_missing_proc_dir(self):
        self.missing_config_key("proc_dir")

    def test_config_missing_accounts(self):
        self.missing_config_key("accounts")

    def test_config_missing_main_codex_home(self):
        self.missing_config_key("accounts.main.codex_home")

    def test_config_missing_main_helper_limit(self):
        self.missing_config_key("accounts.main.helper_limit")

    def test_config_missing_main_plan_deadline(self):
        self.missing_config_key("accounts.main.plan_deadline")

    def test_config_missing_plan(self):
        self.missing_config_key("plan")

    def test_config_missing_plan_path(self):
        self.missing_config_key("plan.path")

    def test_config_missing_plan_columns(self):
        self.missing_config_key("plan.columns")

    def test_config_missing_column_task(self):
        self.missing_config_key("plan.columns.task")

    def test_config_missing_column_thread(self):
        self.missing_config_key("plan.columns.thread")

    def test_config_missing_column_status(self):
        self.missing_config_key("plan.columns.status")

    def test_config_missing_column_account(self):
        self.missing_config_key("plan.columns.account")

    def test_config_missing_ready_prefix(self):
        self.missing_config_key("plan.ready_prefix")

    def test_config_missing_closed_prefixes(self):
        self.missing_config_key("plan.closed_prefixes")

    def test_config_missing_extra_allowed_paths(self):
        self.missing_config_key("extra_allowed_paths")

    def test_config_missing_report_path(self):
        self.missing_config_key("report_path")

    def test_config_missing_signal_path(self):
        self.missing_config_key("signal_path")

    def test_config_missing_thresholds(self):
        self.missing_config_key("thresholds")

    def test_config_missing_window_minutes(self):
        self.missing_config_key("thresholds.window_minutes")

    def test_config_missing_rate_snapshot_hours(self):
        self.missing_config_key("thresholds.rate_snapshot_hours")

    def test_config_missing_underuse_pp(self):
        self.missing_config_key("thresholds.underuse_pp")

    def test_config_missing_idle_minutes(self):
        self.missing_config_key("thresholds.idle_minutes")

    def test_config_missing_waiting_minutes(self):
        self.missing_config_key("thresholds.waiting_minutes")

    def test_config_missing_repeat_count(self):
        self.missing_config_key("thresholds.repeat_count")

    def test_config_missing_stagnation_minutes(self):
        self.missing_config_key("thresholds.stagnation_minutes")

    def test_config_missing_data_grace_minutes(self):
        self.missing_config_key("thresholds.data_grace_minutes")

    def test_config_missing_leader_command_count(self):
        self.missing_config_key("thresholds.leader_command_count")

    def test_config_missing_runtime_seconds(self):
        self.missing_config_key("thresholds.runtime_seconds")

    def test_config_accounts_empty(self):
        f = self.fixture("config-accounts-empty")
        f.config["accounts"] = {}
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_accounts_wrong_type(self):
        f = self.fixture("config-accounts-type")
        f.config["accounts"] = []
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_unknown_key(self):
        f = self.fixture("config-unknown-key")
        f.config["not_a_contract_key"] = 1
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_duplicate_account(self):
        f = self.fixture("config-duplicate-account")
        source = json.dumps(f.config, ensure_ascii=False)
        duplicate = json.dumps(f.config["accounts"]["main"], ensure_ascii=False)
        source = source.replace('"accounts": {', '"accounts": {"main": ' + duplicate + ',', 1)
        f.config_path.write_text(source + "\n", encoding="utf-8")
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_duplicate_root(self):
        f = self.fixture("config-duplicate-root")
        f.config["accounts"]["acct2"]["codex_home"] = f.config["accounts"]["main"]["codex_home"]
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_duplicate_symlink_root(self):
        f = self.fixture("config-symlink-duplicate-root")
        alias = f.root / "accounts" / "alias-main"
        alias.symlink_to(Path(f.config["accounts"]["main"]["codex_home"]), target_is_directory=True)
        f.config["accounts"]["acct2"]["codex_home"] = str(alias)
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_slots_boolean(self):
        f = self.fixture("config-slots-bool")
        f.config["accounts"]["main"]["slots"] = True
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_slots_fractional(self):
        f = self.fixture("config-slots-fraction")
        f.config["accounts"]["main"]["slots"] = 1.5
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_slots_negative(self):
        f = self.fixture("config-slots-negative")
        f.config["accounts"]["main"]["slots"] = -1
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_helper_limit_negative(self):
        f = self.fixture("config-helper-negative")
        f.config["accounts"]["main"]["helper_limit"] = -1
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_helper_limit_fractional(self):
        f = self.fixture("config-helper-fraction")
        f.config["accounts"]["main"]["helper_limit"] = 1.5
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_deadline_wrong_type(self):
        f = self.fixture("config-deadline-type")
        f.config["accounts"]["main"]["plan_deadline"] = "tomorrow"
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_deadline_not_after_window_start(self):
        f = self.fixture("config-deadline-before-window")
        f.config["accounts"]["main"]["plan_deadline"] = f.now - 60
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_positive_threshold_zero(self):
        f = self.fixture("config-threshold-zero")
        f.config["thresholds"]["idle_minutes"] = 0
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_underuse_negative(self):
        f = self.fixture("config-underuse-negative")
        f.config["thresholds"]["underuse_pp"] = -1
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_repeat_count_fractional(self):
        f = self.fixture("config-repeat-fraction")
        f.config["thresholds"]["repeat_count"] = 2.5
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_leader_command_count_negative(self):
        f = self.fixture("config-command-count-negative")
        f.config["thresholds"]["leader_command_count"] = -1
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_path_relative(self):
        f = self.fixture("config-relative-path")
        f.config["tasks_dir"] = "relative/tasks"
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_columns_wrong_type(self):
        f = self.fixture("config-column-type")
        f.config["plan"]["columns"]["task"] = 42
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_closed_prefixes_wrong_type(self):
        f = self.fixture("config-closed-prefix-type")
        f.config["plan"]["closed_prefixes"] = "Принята"
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_closed_prefix_element_wrong_type(self):
        f = self.fixture("config-closed-prefix-element-type")
        f.config["plan"]["closed_prefixes"] = ["Принята", 1]
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_extra_allowed_element_wrong_type(self):
        f = self.fixture("config-extra-path-element-type")
        f.config["extra_allowed_paths"] = ["/tmp", 1]
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_nonnull_key_null(self):
        f = self.fixture("config-nonnull-null")
        f.config["tasks_dir"] = None
        f.write_config()
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_config_report_path_directory(self):
        f = self.fixture("config-report-directory")
        f.report.mkdir()
        result = self.check(f, code=2)
        self.assertTrue(str(f.config_path) in result.stderr or str(f.report) in result.stderr,
                        result.stderr)

    def test_config_signal_path_directory(self):
        f = self.fixture("config-signal-directory")
        f.signal.unlink()
        f.signal.mkdir()
        result = self.check(f, code=2)
        self.assertTrue(str(f.config_path) in result.stderr or str(f.signal) in result.stderr,
                        result.stderr)

    def test_config_existing_plan_read_error(self):
        f = self.fixture("config-plan-directory")
        f.state.unlink()
        f.state.mkdir()
        self.check(f, code=2, diagnostic=(f.state,))

    def test_config_malformed_json(self):
        f = self.fixture("config-malformed-json")
        f.config_path.write_text("{\n", encoding="utf-8")
        self.check(f, code=2, diagnostic=(f.config_path,))

    def test_custom_main_slots(self):
        f = self.fixture("custom-main-slots", main_slots=8)
        for index in range(8):
            session = f"{index + 1:08d}-2222-4222-8222-{index + 1:012d}"
            f.add_run(f"main-{index}", "main", session, age_minutes=1, live_pid=True,
                      events=self.fresh_events(session, now=f.now), pid=44000 + index)
        f.set_state([("ready", "", "Готова к запуску")])
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["accounts"]["main"]["slots"] = 9
        f.write_config()
        self.check(f, [self.ready_line("main", 1, 1, f)], 3,
                   initial_signal=initial)

    def test_custom_acct2_slots(self):
        f = self.fixture("custom-acct2-slots", main_slots=1, acct2_slots=4)
        self.healthy_run(f, account="main")
        for index in range(4):
            session = f"{index + 20:08d}-2222-4222-8222-{index + 20:012d}"
            f.add_run(f"acct2-{index}", "acct2", session, age_minutes=1, live_pid=True,
                      events=self.fresh_events(session, now=f.now), pid=44100 + index)
        f.set_state([("ready", "", "Готова к запуску")])
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["accounts"]["acct2"]["slots"] = 5
        f.write_config()
        self.check(f, [self.ready_line("acct2", 1, 1, f)], 3, initial_signal=initial)

    def test_custom_helper_limit(self):
        f = self.fixture("custom-helper-limit", main_slots=1)
        rollout, _ = self.make_mechanics(f, 21, [(10, 9)] * 2)
        line = f"SIGNAL 4б 11111111-1111-4111-8111-111111111111: дроби; команд=21; порог=20; максимум помощников на интервале=2; предел=60; источник={rollout}:44"
        self.check(f, [line], 3)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["accounts"]["main"]["helper_limit"] = 2
        f.write_config()
        self.check(f, ["OK"], 0, initial_signal=initial)

    def test_custom_window_minutes(self):
        f = self.fixture("custom-window", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 32 * 60, "pytest", 1, "", "old"),
                  command_event(session, now - 27 * 60, "pytest", 1, "", "middle"),
                  command_event(session, now - 26 * 60, "pytest", 1, "", "recent"),
                  assistant_event(session, now - 60, "Свежая активность")]
        rollout = f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["window_minutes"] = 40
        f.write_config()
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:4"
        self.check(f, [line], 3, initial_signal=initial)

    def test_custom_rate_snapshot_hours(self):
        f = self.fixture("custom-rate-snapshot-hours")
        old = f.now - 5 * 3600 - 58 * 60
        f.write_quota("main", epoch=old, primary_used=0, primary_window=10080,
                      primary_reset=old + 10080 * 60, mtime=f.now)
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["rate_snapshot_hours"] = 5
        f.write_config()
        self.check(f, [self.no_snapshot_line("main", 5, f)], 3, initial_signal=initial)

    def test_custom_underuse_pp(self):
        f = self.fixture("custom-underuse-pp")
        snapshot = f.now - 1
        f.write_quota("main", primary_used=39, primary_window=300, primary_reset=snapshot + 150 * 60,
                      secondary_used=0, secondary_window=10080, epoch=snapshot)
        source = f.rollouts[next(key for key in f.rollouts if key.endswith("0001"))]
        line = f"SIGNAL 1б main: primary: расход=39.00%; ожидается=50.00%; отставание=11.00 п.п.; источник={source}:2"
        self.check(f, [line], 3)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["underuse_pp"] = 11
        f.write_config()
        self.check(f, ["OK"], 0, initial_signal=initial)

    def test_custom_idle_minutes(self):
        f = self.fixture("custom-idle-minutes", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 18 * 60
        rollout = f.write_rollout("main", session, [assistant_event(session, event_time, "Тихая работа")])
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["idle_minutes"] = 16
        f.write_config()
        line = f"SIGNAL 2а {session}: простой; без событий={AGE} мин; порог=16.00 мин; источник={rollout}:2"
        self.check(f, [line], 3, ages=(event_time,), initial_signal=initial)

    def test_custom_waiting_minutes(self):
        f = self.fixture("custom-waiting-minutes")
        session = "11111111-1111-4111-8111-111111111111"
        finished = f.now - 13 * 60
        run, _ = f.add_run("finished", "main", session, age_minutes=30, status=finished,
                           live_pid=False, events=[assistant_event(session, finished - 60, "Завершено")])
        f.set_state([("task", session, "В работе")])
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["waiting_minutes"] = 11
        f.write_config()
        line = f"SIGNAL 2б {session}: ожидание; после finished={AGE} мин; порог=11.00 мин; источник={run / 'status'}"
        self.check(f, [line], 3, ages=(finished,), initial_signal=initial)

    def test_custom_repeat_count(self):
        f = self.fixture("custom-repeat-count", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        rollout = f.write_rollout("main", session, self.cycle_events(session, count=2, now=f.now))
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["repeat_count"] = 2
        f.write_config()
        line = f"SIGNAL 3а {session}: цикл; попыток=2; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:3"
        self.check(f, [line], 3, initial_signal=initial)

    def test_custom_stagnation_minutes(self):
        f = self.fixture("custom-stagnation-minutes", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        f.write_rollout("main", session, [assistant_event(session, f.now - 60, "Без строки хода")])
        _, start_time = f.add_run("leader", "main", session, age_minutes=118, live_pid=True, events=None)
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["stagnation_minutes"] = 116
        f.write_config()
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 116.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,), initial_signal=initial)

    def test_custom_data_grace_minutes(self):
        f = self.fixture("custom-data-grace", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        run, start_time = self.healthy_run(f, age_minutes=7)
        f.state.unlink()
        line = self.missing_line(session, "leader", "источник плана", source=f.state)
        self.check(f, [line], 3, ages=(start_time,))
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["data_grace_minutes"] = 9
        f.write_config()
        self.check(f, [self.pending_line("leader", "источник плана", f.state)], 0, initial_signal=initial)

    def test_custom_leader_command_count(self):
        f = self.fixture("custom-leader-command-count", main_slots=1)
        rollout, _ = self.make_mechanics(f, 19, [])
        self.check(f, ["OK"], 0)
        initial = f.signal.read_text(encoding="utf-8")
        f.config["thresholds"]["leader_command_count"] = 18
        f.write_config()
        line = f"SIGNAL 4б 11111111-1111-4111-8111-111111111111: дроби; команд=19; порог=18; максимум помощников на интервале=0; предел=60; источник={rollout}:40"
        self.check(f, [line], 3, initial_signal=initial)

    def test_snapshot_t60_uses_snapshot_time(self):
        f = self.fixture("snapshot-t60")
        snapshot = f.now - 60 * 60
        f.write_quota("main", primary_used=40, primary_window=300, primary_reset=f.now + 120 * 60,
                      secondary=False, epoch=snapshot)
        self.check(f, ["OK"], 0)

    def test_unrelated_live_proc_is_not_child(self):
        f = self.fixture("unrelated-proc-not-child", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        event_time = f.now - 22 * 60
        rollout = f.write_rollout("main", session,
                                  [command_event(session, event_time, "pytest", status="running", item_id="running")])
        f.add_run("leader", "main", session, age_minutes=22, live_pid=True, events=None)
        (f.proc / "43010" / "task" / "43010").mkdir(parents=True)
        (f.proc / "43010" / "task" / "43010" / "children").write_text("43011\n", encoding="utf-8")
        (f.proc / "43011" / "task" / "43011").mkdir(parents=True)
        (f.proc / "43011" / "task" / "43011" / "children").write_text("", encoding="utf-8")
        self.check(f, [self.idle_line(session, rollout)], 3, ages=(event_time,))

    def test_helper_own_pending_command_and_child_exempt_idle(self):
        f = self.fixture("helper-pending-command-child", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent = [subagent_event(leader, now - 22 * 60, "started", helper),
                  assistant_event(leader, now - 60, "Руководитель продолжает")]
        self.healthy_run(f, session_id=leader)
        f.write_rollout("main", leader, parent)
        event_time = now - 22 * 60
        helper_rollout = f.write_rollout("main", helper,
                                         [task_boundary_event("task_started", event_time, "helper-turn"),
                                          command_event(helper, event_time, "pytest", status="running", item_id="running")],
                                         parent_id=leader, history_start=1)
        folder = f.tasks / "leader"
        pid = int((folder / "started").read_text(encoding="utf-8").split("pid=")[1])
        children = f.proc / str(pid) / "task" / str(pid) / "children"
        children.write_text("43012\n", encoding="utf-8")
        (f.proc / "43012" / "task" / "43012").mkdir(parents=True)
        (f.proc / "43012" / "task" / "43012" / "children").write_text("", encoding="utf-8")
        self.check(f, ["OK"], 0)

    def test_cycle_different_exit_prevents_repeat(self):
        f = self.fixture("cycle-different-exit", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "a"),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "b"),
                  command_event(session, now - 8 * 60, "pytest", 0, "", "c"),
                  assistant_event(session, now - 60, "Свежая активность")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_cycle_different_output_prevents_repeat(self):
        f = self.fixture("cycle-different-output", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "a"),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "b"),
                  command_event(session, now - 8 * 60, "pytest", 1, "x\n", "c"),
                  assistant_event(session, now - 60, "Свежая активность")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_cycle_duplicate_item_id_counts_once(self):
        f = self.fixture("cycle-duplicate-item-id", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        first = command_event(session, now - 10 * 60, "pytest", 1, "", "same-a")
        second = command_event(session, now - 9 * 60, "pytest", 1, "", "same-b")
        events = [first, second, second, assistant_event(session, now - 60, "Свежая активность")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_cycle_response_and_result_count_once(self):
        f = self.fixture("cycle-response-result-once", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = []
        for index, minute in ((1, 10), (2, 9)):
            call_id = f"call-{index}"
            events.append(event_record("response_item", now - minute * 60, {
                "type": "custom_tool_call", "name": "exec", "call_id": call_id,
                "input": "tools.exec_command({command: 'pytest'})",
            }))
            events.append(command_event(session, now - minute * 60 + 1, ["/bin/bash", "-lc", "pytest"],
                                        1, "", call_id))
        events.append(assistant_event(session, now - 60, "Свежая активность"))
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_future_event_does_not_replace_current(self):
        f = self.fixture("future-rate-event")
        session_id = "00000000-0000-4000-8000-000000000001"
        current = f.now - 1
        current_primary = {"used_percent": 39, "window_minutes": 300, "resets_at": current + 150 * 60}
        current_secondary = {"used_percent": 0, "window_minutes": 10080, "resets_at": current + 10080 * 60}
        future_primary = {"used_percent": 0, "window_minutes": 10080, "resets_at": f.now + 10080 * 60}
        events = [rate_event(current, current_primary, current_secondary),
                  rate_event(f.now + 60, future_primary, None)]
        rollout = f.write_rollout("main", session_id, events)
        line = f"SIGNAL 1б main: primary: расход=39.00%; ожидается=50.00%; отставание=11.00 п.п.; источник={rollout}:2"
        self.check(f, [line], 3)

    def test_latest_rate_timestamp_wins_over_row_and_mtime_order(self):
        f = self.fixture("latest-rate-timestamp")
        default_id = "00000000-0000-4000-8000-000000000001"
        f.rollouts[default_id].unlink()
        del f.rollouts[default_id]
        newer_id = "88888888-8888-4888-8888-888888888888"
        older_id = "99999999-9999-4999-8999-999999999999"
        newer = f.now - 60 * 60
        older = f.now - 2 * 60 * 60
        healthy = {"used_percent": 0, "window_minutes": 300, "resets_at": newer + 300 * 60}
        underused = {"used_percent": 39, "window_minutes": 300, "resets_at": older + 150 * 60}
        f.write_rollout("main", newer_id,
                        [rate_event(newer, healthy, None), rate_event(older, underused, None)],
                        mtime=f.now - 4 * 3600)
        f.write_rollout("main", older_id, [rate_event(older, underused, None)], mtime=f.now - 30 * 60)
        self.check(f, ["OK"], 0)

    def test_elapsed_deadline_clamps_expected_to_100(self):
        f = self.fixture("elapsed-deadline-clamp")
        snapshot = f.now - 60 * 60
        f.write_quota("main", primary_used=89, primary_window=300,
                      primary_reset=f.now + 120 * 60, secondary=False, epoch=snapshot)
        f.config["accounts"]["main"]["plan_deadline"] = f.now - 90 * 60
        f.write_config()
        source = f.rollouts[next(key for key in f.rollouts if key.endswith("0001"))]
        line = f"SIGNAL 1б main: primary: расход=89.00%; ожидается=100.00%; отставание=11.00 п.п.; источник={source}:2"
        self.check(f, [line], 3)

    def test_progress_blockquote_is_not_own_step(self):
        f = self.fixture("progress-blockquote", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        f.write_rollout("main", session, [assistant_event(session, f.now - 60, "> ХОД: [x] Цитата")])
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_progress_fenced_code_is_not_own_step(self):
        f = self.fixture("progress-fenced-code", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        text = "```text\nХОД: [x] Код\n```"
        f.write_rollout("main", session, [assistant_event(session, f.now - 60, text)])
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_progress_whitespace_repeat_is_not_new_step(self):
        f = self.fixture("progress-whitespace-repeat", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        f.write_rollout("main", session, [assistant_event(session, f.now - 180 * 60, "ХОД: [x] Один шаг"),
                                           assistant_event(session, f.now - 10 * 60, "ХОД: [x]   Один   шаг  ")])
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_incomplete_final_jsonl_line_is_ignored(self):
        f = self.fixture("incomplete-jsonl-line")
        session_id = "00000000-0000-4000-8000-000000000001"
        rollout = f.rollouts[session_id]
        with rollout.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"event_msg"')
        self.check(f, ["OK"], 0)

    def test_ready_row_with_running_uuid_is_excluded(self):
        f = self.fixture("ready-running-uuid", main_slots=2)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session)
        f.set_state([("task", session, "Готова к запуску")])
        self.check(f, [self.split_line("main", 1, 0, f)], 3)

    def test_ready_duplicate_task_id_counts_once(self):
        f = self.fixture("ready-duplicate-task-id", main_slots=2)
        f.set_state([("same-task", "", "Готова к запуску"), ("same-task", "", "Готова к запуску")])
        self.check(f, [self.ready_line("main", 2, 1, f), self.split_line("main", 2, 1, f)], 3)

    def test_waiting_text_does_not_make_ready(self):
        f = self.fixture("waiting-not-ready", main_slots=1)
        f.set_state([("task", "", "Ждёт; следующий шаг — запуск" )])
        self.check(f, [self.split_line("main", 1, 0, f)], 3)

    def test_completed_status_is_not_orphan(self):
        f = self.fixture("completed-status-not-orphan", main_slots=1)
        live_session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=live_session)
        session = "22222222-2222-4222-8222-222222222222"
        finished = f.now - 60
        f.add_run("completed", "main", session, age_minutes=10, status=finished, live_pid=False,
                  events=[assistant_event(session, finished - 60, "Завершено")])
        self.check(f, ["OK"], 0)

    def test_nested_helper_own_history_is_attributed(self):
        f = self.fixture("nested-helper-cycle", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        nested = "55555555-5555-4555-8555-555555555555"
        now = f.now
        parent_events = [subagent_event(leader, now - 11 * 60, "started", helper),
                         assistant_event(leader, now - 60, "Руководитель продолжает")]
        self.healthy_run(f, session_id=leader)
        f.write_rollout("main", leader, parent_events)
        helper_events = [subagent_event(helper, now - 11 * 60, "started", nested),
                         assistant_event(helper, now - 60, "Помощник продолжает")]
        f.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        nested_events = [task_boundary_event("task_started", now - 11 * 60, "nested-turn")]
        nested_events.extend(self.cycle_events(nested, count=3, now=now))
        nested_rollout = f.write_rollout("main", nested, nested_events, parent_id=helper, history_start=1)
        line = f"SIGNAL 3а {nested}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={nested_rollout}:5"
        self.check(f, [line], 3)

    def test_helper_missing_journal_uses_grace(self):
        f = self.fixture("helper-missing-journal", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        started = f.now - 3 * 60
        self.healthy_run(f, session_id=leader, age_minutes=30)
        f.write_rollout("main", leader, [subagent_event(leader, started, "started", helper),
                                           assistant_event(leader, f.now - 60, "Руководитель продолжает")])
        source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{helper}.jsonl"
        self.check(f, [self.pending_line("leader", "журнал", source)], 0)

    def test_insufficient_history_is_not_empty_progress(self):
        f = self.fixture("insufficient-history", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=None)
        home = Path(f.accounts["main"]["codex_home"])
        old_epoch = f.now - 8 * 3600
        old_day = datetime.fromtimestamp(old_epoch, timezone.utc)
        old_folder = home / "sessions" / old_day.strftime("%Y") / old_day.strftime("%m") / old_day.strftime("%d")
        old_folder.mkdir(parents=True, exist_ok=True)
        old_path = old_folder / f"rollout-{utc_stamp(old_epoch).replace(':', '-')}-{session}.jsonl"
        old_text = "ХОД: [x] Исторический шаг"
        old_path.write_text(json_line(task_meta(session)) +
                            json_line(assistant_event(session, old_epoch, old_text)), encoding="utf-8")
        os.utime(old_path, (old_epoch, old_epoch))
        rollout = f.write_rollout("main", session, [assistant_event(session, f.now - 60, old_text)],
                                  mtime=f.now - 10 * 60)
        line = self.missing_line(session, "leader", "история", source=rollout)
        self.check(f, [line], 3, ages=(start_time,))

    def test_session_symlink_does_not_expand_read_roots(self):
        f = self.fixture("session-symlink-read-root")
        home_sessions = Path(f.accounts["main"]["codex_home"]) / "sessions"
        foreign_id = "66666666-6666-4666-8666-666666666666"
        snapshot = f.now
        foreign_home = f.root / "outside-account" / "sessions" / "2026" / "11" / "05"
        foreign_home.mkdir(parents=True)
        foreign = foreign_home / f"rollout-{utc_stamp(snapshot).replace(':', '-')}-{foreign_id}.jsonl"
        primary = {"used_percent": 39, "window_minutes": 300, "resets_at": snapshot + 150 * 60}
        secondary = {"used_percent": 0, "window_minutes": 10080, "resets_at": snapshot + 10080 * 60}
        foreign.write_text(json_line(task_meta(foreign_id)) +
                           json_line(rate_event(snapshot, primary, secondary)), encoding="utf-8")
        alias = home_sessions / "2026" / "11" / "05" / foreign.name
        alias.symlink_to(foreign)
        os.utime(foreign, (f.now, f.now))
        self.check(f, ["OK"], 0)

    def test_236_identical_signal_set_uses_repeat_line(self):
        self.repeat_sequence("repeat-identical-signal-set")

    def test_236_third_repeat_points_to_first_full_block(self):
        self.repeat_sequence("repeat-third-points-first")

    def test_236_changed_signal_set_starts_new_block(self):
        self.repeat_sequence("repeat-changed-signal-set")

    def test_237_cycle_window_28m_includes_attempts(self):
        f = self.fixture("cycle-window-28m", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 28 * 60, "pytest", 1, "", "a"),
                  command_event(session, now - 27 * 60, "pytest", 1, "", "b"),
                  command_event(session, now - 26 * 60, "pytest", 1, "", "c"),
                  assistant_event(session, now - 60, "Свежая активность")]
        rollout = f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_237_cycle_window_32m_excludes_old_attempt(self):
        f = self.fixture("cycle-window-32m", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        events = [command_event(session, now - 32 * 60, "pytest", 1, "", "old"),
                  command_event(session, now - 27 * 60, "pytest", 1, "", "middle"),
                  command_event(session, now - 26 * 60, "pytest", 1, "", "recent"),
                  assistant_event(session, now - 60, "Свежая активность")]
        f.write_rollout("main", session, events)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        self.check(f, ["OK"], 0)

    def test_237_rate_snapshot_mtime_4h_is_selected(self):
        f = self.fixture("rate-mtime-4h")
        f.write_quota("main", primary_used=0, primary_window=10080,
                      primary_reset=f.now - 1 + 10080 * 60, mtime=f.now - 4 * 3600)
        self.check(f, ["OK"], 0)

    def test_237_old_unrelated_rollout_is_not_read(self):
        f = self.fixture("unrelated-rollout-old-mtime")
        unrelated_id = "77777777-7777-4777-8777-777777777777"
        unrelated = Path(f.accounts["main"]["codex_home"]) / "sessions" / "2026" / "11" / "05" / f"rollout-2026-11-04T00-00-00-{unrelated_id}.jsonl"
        snapshot = f.now
        primary = {"used_percent": 39, "window_minutes": 300, "resets_at": snapshot + 150 * 60}
        unrelated.write_text(json_line(task_meta(unrelated_id)) +
                              json_line(rate_event(snapshot, primary, None)), encoding="utf-8")
        os.utime(unrelated, (f.now - 8 * 3600, f.now - 8 * 3600))
        self.check(f, ["OK"], 0)

    def test_pending_change_does_not_change_load_deduplication(self):
        f = self.fixture("pending-change-load-repeat", main_slots=0)
        orphan_id = "22222222-2222-4222-8222-222222222222"
        orphan, _ = f.add_run("orphan", "main", orphan_id, age_minutes=1, live_pid=False,
                              events=self.fresh_events(orphan_id, now=f.now), pid=42002)
        signal_line = f"SIGNAL 1а main: оборванный прогон; прогон=orphan; pid=42002; источник={orphan / 'started'}"
        self.check(f, [signal_line], 3, signal_lines=[signal_line])
        initial = f.signal.read_text(encoding="utf-8")
        first_stamp = re.search(r"## LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\n", initial).group(1)
        time.sleep(1.1)
        session = "11111111-1111-4111-8111-111111111111"
        f.add_run("pending", "main", session, age_minutes=3, live_pid=True, events=None)
        pending_source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"
        expected = [self.pending_line("pending", "журнал", pending_source), signal_line]
        before_repeat = f.signal.read_text(encoding="utf-8")
        result, started, finished = f.run()
        self.assert_repeat_output(f, result, started, finished, expected, before_repeat, first_stamp)

    def test_238_existing_input_timeout_is_error2_with_path_and_limit(self):
        f = self.fixture("fifo-timeout", alphabetic_path=True)
        self.assertNotIn("1", str(f.state))
        f.state.unlink()
        os.mkfifo(f.state)
        f.config["thresholds"]["runtime_seconds"] = 1
        f.write_config()
        result = self.check(f, code=2, diagnostic=(f.state,))
        self.assertRegex(result.stderr, r"(?<![0-9])1(?![0-9])")

    def test_239_inputs_unchanged_and_only_report_signal_written(self):
        f = self.fixture("inputs-unchanged", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session)
        orphan, _ = f.add_run("orphan", "main", session, age_minutes=1, live_pid=False, events=None, pid=42002)
        line = f"SIGNAL 1а main: оборванный прогон; прогон=orphan; pid=42002; источник={orphan / 'started'}"
        excluded = {f.report, f.signal}
        def inputs_snapshot():
            snapshot = {}
            for path in sorted(f.root.rglob("*")):
                if path in excluded:
                    continue
                if path.is_symlink():
                    value = ("symlink", os.readlink(path))
                elif path.is_dir():
                    value = ("directory",)
                elif path.is_file():
                    value = ("file", path.read_bytes())
                else:
                    value = ("other", path.lstat().st_mode, path.lstat().st_size)
                snapshot[path.relative_to(f.root)] = value
            return snapshot
        before = inputs_snapshot()
        self.assertIn(Path("home"), before)
        self.assertIn(Path("tmpdir"), before)
        tools_before = self.object_snapshot(ROOT / "tools")
        self.check(f, [line], 3)
        after = inputs_snapshot()
        self.assertEqual(after, before)
        self.assertEqual(self.object_snapshot(ROOT / "tools"), tools_before)

    def test_239_no_pyc_after_successful_cli(self):
        f = self.fixture("no-pyc-after-success")
        copied_tools = f.root / "tools-copy"
        shutil.copytree(ROOT / "tools", copied_tools, symlinks=True,
                        ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))

        def cache_snapshot():
            return {path.relative_to(copied_tools) for path in copied_tools.rglob("*")
                    if path.name.endswith(".pyc") or "__pycache__" in path.parts}

        self.assertEqual(cache_snapshot(), set())
        started = time.time()
        result = subprocess.run(
            ["python3", "-I", str(copied_tools / "codex-load-check.py"), "--config", str(f.config_path)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30, env=f.environment(),
        )
        finished = time.time()
        self.assert_output(f, result, started, finished, ["OK"], 0)
        self.assertEqual(cache_snapshot(), set())

        thread_id = "77777777-7777-4777-8777-777777777777"
        watch_sessions = f.root / "watch-sessions"
        watch_folder = watch_sessions / "records"
        watch_folder.mkdir(parents=True)
        event_time = int(time.time()) - 1
        watch_path = watch_folder / f"rollout-{utc_stamp(event_time).replace(':', '-')}-{thread_id}.jsonl"
        watch_path.write_text(json_line(task_meta(thread_id)) +
                              json_line(command_event(thread_id, event_time, "echo watcher", 0, "",
                                                      "watcher-command")), encoding="utf-8")
        watch_started = time.time()
        watch_result = subprocess.run(
            ["python3", "-I", str(copied_tools / "codex-watch.py"), "--thread", thread_id,
             "--sessions", str(watch_sessions), "--minutes", "60"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30, env=f.environment(),
        )
        watch_finished = time.time()
        self.assertEqual(watch_result.returncode, 0, watch_result.stderr)
        start_hm = datetime.fromtimestamp(watch_started, timezone.utc).strftime("%H:%M")
        since_start_hm = datetime.fromtimestamp(watch_started - 3600, timezone.utc).strftime("%H:%M")
        finish_hm = datetime.fromtimestamp(watch_finished, timezone.utc).strftime("%H:%M")
        since_finish_hm = datetime.fromtimestamp(watch_finished - 3600, timezone.utc).strftime("%H:%M")
        first_lines = {f"window {since_start_hm}-{start_hm} UTC\n",
                       f"window {since_finish_hm}-{finish_hm} UTC\n"}
        suffix = "\n[LEAD] idle 0 min; commands 1; failed 0; compactions 0; edits 0; collab calls 0\n"
        self.assertTrue(watch_result.stdout.endswith(suffix), watch_result.stdout)
        first_line = watch_result.stdout[:-len(suffix)]
        self.assertIn(first_line, first_lines)
        self.assertEqual(cache_snapshot(), set())
        self.assertEqual(f.report.read_text(encoding="utf-8"), "OK\n")
        self.assertEqual(f.signal.read_text(encoding="utf-8"), "existing signal\n")

    def test_239_example_config_schema_and_values(self):
        path = ROOT / "tools" / "codex-load-check.example.json"
        self.assertTrue(path.is_file(), str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual(set(data), {"tasks_dir", "proc_dir", "accounts", "plan", "extra_allowed_paths",
                                     "report_path", "signal_path", "thresholds"})
        for key in ("tasks_dir", "proc_dir", "report_path", "signal_path"):
            self.assertIs(type(data[key]), str)
        self.assertTrue(os.path.isabs(data["tasks_dir"]))
        self.assertTrue(os.path.isabs(data["proc_dir"]))
        self.assertTrue(os.path.isabs(data["report_path"]))
        self.assertTrue(os.path.isabs(data["signal_path"]))
        self.assertEqual(data["tasks_dir"], "/path/to/launcher/tasks")
        self.assertEqual(data["proc_dir"], "/proc")
        self.assertIsInstance(data["accounts"], dict)
        self.assertTrue(data["accounts"])
        for account in data["accounts"].values():
            self.assertIsInstance(account, dict)
            self.assertEqual(set(account), {"codex_home", "slots", "helper_limit", "plan_deadline"})
            self.assertIs(type(account["codex_home"]), str)
            self.assertTrue(os.path.isabs(account["codex_home"]))
            self.assertEqual(account["codex_home"], "/path/to/codex-home")
            self.assertIs(type(account["slots"]), int)
            self.assertEqual(account["slots"], 1)
            self.assertIs(type(account["helper_limit"]), int)
            self.assertEqual(account["helper_limit"], 60)
            self.assertIsNone(account["plan_deadline"])
        self.assertIsInstance(data["plan"], dict)
        self.assertEqual(set(data["plan"]), {"path", "columns", "ready_prefix", "closed_prefixes"})
        self.assertIs(type(data["plan"]["path"]), str)
        self.assertTrue(os.path.isabs(data["plan"]["path"]))
        self.assertEqual(data["plan"]["path"], "/path/to/STATE.md")
        self.assertIsInstance(data["plan"]["columns"], dict)
        self.assertEqual(data["plan"]["columns"], {"task": "Задача", "thread": "Нить",
                                                     "status": "Его статус", "account": None})
        self.assertEqual(data["plan"]["ready_prefix"], "Готова к запуску")
        self.assertIs(type(data["plan"]["ready_prefix"]), str)
        self.assertIsInstance(data["plan"]["closed_prefixes"], list)
        self.assertTrue(all(type(value) is str for value in data["plan"]["closed_prefixes"]))
        self.assertEqual(data["plan"]["closed_prefixes"], ["Принята", "Сохранена", "Закрыта"])
        self.assertIsInstance(data["extra_allowed_paths"], list)
        self.assertTrue(all(type(value) is str for value in data["extra_allowed_paths"]))
        self.assertEqual(data["extra_allowed_paths"], ["/tmp"])
        self.assertEqual(data["report_path"], "/path/to/load-report.txt")
        self.assertEqual(data["signal_path"], "/path/to/load-signal.md")
        self.assertIsInstance(data["thresholds"], dict)
        thresholds = data["thresholds"]
        self.assertEqual(set(thresholds), {"window_minutes", "rate_snapshot_hours", "underuse_pp", "idle_minutes",
                                           "waiting_minutes", "repeat_count", "stagnation_minutes",
                                           "data_grace_minutes", "leader_command_count", "runtime_seconds"})
        for key, value in (("window_minutes", 30), ("rate_snapshot_hours", 6), ("idle_minutes", 20),
                           ("waiting_minutes", 15), ("stagnation_minutes", 120),
                           ("data_grace_minutes", 5), ("runtime_seconds", 60)):
            self.assertIn(type(thresholds[key]), (int, float))
            self.assertNotIsInstance(thresholds[key], bool)
            self.assertGreater(thresholds[key], 0)
            self.assertEqual(thresholds[key], value)
        self.assertIn(type(thresholds["underuse_pp"]), (int, float))
        self.assertNotIsInstance(thresholds["underuse_pp"], bool)
        self.assertEqual(thresholds["underuse_pp"], 10)
        self.assertIs(type(thresholds["repeat_count"]), int)
        self.assertEqual(thresholds["repeat_count"], 3)
        self.assertIs(type(thresholds["leader_command_count"]), int)
        self.assertEqual(thresholds["leader_command_count"], 20)

    def test_239_project_config_and_units_match_plan(self):
        config_path = Path("/srv/novgorod-work/tools/codex-load-check/config.json")
        self.assertTrue(config_path.is_file(), str(config_path))
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertEqual(set(data), {"tasks_dir", "proc_dir", "accounts", "plan", "extra_allowed_paths",
                                     "report_path", "signal_path", "thresholds"})
        for key in ("tasks_dir", "proc_dir", "report_path", "signal_path"):
            self.assertIs(type(data[key]), str)
            self.assertTrue(os.path.isabs(data[key]))
        self.assertEqual(data["tasks_dir"], "/srv/novgorod-work/codex-direct/tasks")
        self.assertEqual(data["proc_dir"], "/proc")
        self.assertIsInstance(data["accounts"], dict)
        self.assertEqual(set(data["accounts"]), {"main", "acct2"})
        for account in data["accounts"].values():
            self.assertIsInstance(account, dict)
            self.assertEqual(set(account), {"codex_home", "slots", "helper_limit", "plan_deadline"})
            self.assertIs(type(account["codex_home"]), str)
            self.assertTrue(os.path.isabs(account["codex_home"]))
            self.assertIs(type(account["slots"]), int)
            self.assertIs(type(account["helper_limit"]), int)
            self.assertIsNone(account["plan_deadline"])
        self.assertEqual(data["accounts"]["main"], {
            "codex_home": "/srv/novgorod-work/codex-direct/home", "slots": 8,
            "helper_limit": 60, "plan_deadline": None,
        })
        self.assertEqual(data["accounts"]["acct2"], {
            "codex_home": "/srv/novgorod-work/codex-home/acct2", "slots": 4,
            "helper_limit": 60, "plan_deadline": None,
        })
        self.assertIsInstance(data["plan"], dict)
        self.assertEqual(set(data["plan"]), {"path", "columns", "ready_prefix", "closed_prefixes"})
        self.assertIs(type(data["plan"]["path"]), str)
        self.assertTrue(os.path.isabs(data["plan"]["path"]))
        self.assertIsInstance(data["plan"]["columns"], dict)
        self.assertEqual(set(data["plan"]["columns"]), {"task", "thread", "status", "account"})
        self.assertIs(type(data["plan"]["columns"]["task"]), str)
        self.assertIs(type(data["plan"]["columns"]["thread"]), str)
        self.assertIs(type(data["plan"]["columns"]["status"]), str)
        self.assertIsNone(data["plan"]["columns"]["account"])
        self.assertIs(type(data["plan"]["ready_prefix"]), str)
        self.assertIsInstance(data["plan"]["closed_prefixes"], list)
        self.assertTrue(all(type(value) is str for value in data["plan"]["closed_prefixes"]))
        self.assertEqual(data["plan"], {
            "path": "/srv/novgorod-work/worktrees/deputy/out/STATE.md",
            "columns": {"task": "Задача", "thread": "Нить", "status": "Его статус", "account": None},
            "ready_prefix": "Готова к запуску", "closed_prefixes": ["Принята", "Сохранена", "Закрыта"],
        })
        self.assertIsInstance(data["extra_allowed_paths"], list)
        self.assertTrue(all(type(value) is str for value in data["extra_allowed_paths"]))
        self.assertEqual(data["extra_allowed_paths"], ["/tmp"])
        self.assertEqual(data["report_path"], "/srv/novgorod-work/tools/codex-load-check/report.txt")
        self.assertEqual(data["signal_path"], "/srv/novgorod-work/worktrees/deputy/in/load-signal.md")
        self.assertIsInstance(data["thresholds"], dict)
        self.assertEqual(set(data["thresholds"]), {"window_minutes", "rate_snapshot_hours", "underuse_pp",
                                                    "idle_minutes", "waiting_minutes", "repeat_count",
                                                    "stagnation_minutes", "data_grace_minutes",
                                                    "leader_command_count", "runtime_seconds"})
        for key in ("window_minutes", "rate_snapshot_hours", "underuse_pp", "idle_minutes", "waiting_minutes",
                    "stagnation_minutes", "data_grace_minutes", "runtime_seconds"):
            self.assertIn(type(data["thresholds"][key]), (int, float))
            self.assertNotIsInstance(data["thresholds"][key], bool)
        self.assertIs(type(data["thresholds"]["repeat_count"]), int)
        self.assertIs(type(data["thresholds"]["leader_command_count"]), int)
        self.assertEqual(data["thresholds"], {
            "window_minutes": 30, "rate_snapshot_hours": 6, "underuse_pp": 10,
            "idle_minutes": 20, "waiting_minutes": 15, "repeat_count": 3,
            "stagnation_minutes": 120, "data_grace_minutes": 5,
            "leader_command_count": 20, "runtime_seconds": 60,
        })
        service_path = Path("/srv/novgorod-work/tools/codex-load-check/codex-load-check.service")
        timer_path = Path("/srv/novgorod-work/tools/codex-load-check/codex-load-check.timer")
        self.assertTrue(service_path.is_file(), str(service_path))
        self.assertTrue(timer_path.is_file(), str(timer_path))
        service = service_path.read_text(encoding="utf-8")
        timer = timer_path.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service.splitlines())
        self.assertIn("SuccessExitStatus=3", service.splitlines())
        exec_lines = [line.split("=", 1)[1] for line in service.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(exec_lines), 1)
        exec_line = exec_lines[0]
        parts = exec_line.split()
        self.assertEqual(len(parts), 7)
        self.assertTrue(Path(parts[0]).is_absolute())
        self.assertEqual(Path(parts[0]).name, "timeout")
        self.assertEqual(parts[1], "120")
        self.assertTrue(Path(parts[2]).is_absolute())
        self.assertEqual(Path(parts[2]).name, "python3")
        self.assertEqual(parts[3], "-I")
        self.assertEqual(parts[4], str(ROOT / "tools" / "codex-load-check.py"))
        self.assertEqual(parts[5], "--config")
        self.assertEqual(parts[6], str(config_path))
        self.assertIn("OnCalendar=*:07,37", timer.splitlines())
        explicit_unit = next((line.split("=", 1)[1] for line in timer.splitlines() if line.startswith("Unit=")), None)
        if explicit_unit is not None:
            self.assertEqual(explicit_unit, service_path.name)
        else:
            self.assertEqual(service_path.stem, timer_path.stem)

    def test_239_readme_has_one_tool_entry(self):
        lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        start = next(index for index, line in enumerate(lines) if re.match(r"^#+\s+Состав\b", line))
        end = next((index for index in range(start + 1, len(lines)) if re.match(r"^#+\s+", lines[index])), len(lines))
        entries = [line for line in lines[start + 1:end] if "codex-load-check" in line]
        self.assertEqual(len(entries), 1)

    def test_213_edit_between_attempts_breaks_series(self):
        f = self.fixture("cycle-edit-between-first-second", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        path = f.work / "tracked.txt"
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                  file_change_event(session, now - 9 * 60 - 30, path, turn_id="leader-turn"),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        self.check(f, ["OK"], 0)

    def test_213_edit_before_first_attempt_does_not_break_series(self):
        f = self.fixture("cycle-edit-before-first", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        path = f.work / "tracked.txt"
        events = [file_change_event(session, now - 11 * 60, path, turn_id="leader-turn"),
                  command_event(session, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:5"
        self.check(f, [line], 3)

    def test_213_edit_after_last_attempt_does_not_break_series(self):
        f = self.fixture("cycle-edit-after-last", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        path = f.work / "tracked.txt"
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                  file_change_event(session, now - 7 * 60, path, turn_id="leader-turn"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_213_helper_edit_breaks_parent_cycle(self):
        f = self.fixture("cycle-helper-edit-breaks-parent", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 11 * 60, "started", helper),
                         command_event(leader, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                         command_event(leader, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                         command_event(leader, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                         assistant_event(leader, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", leader, age_minutes=1, live_pid=True, events=parent_events)
        helper_events = [task_boundary_event("task_started", now - 9 * 60 - 30, "helper-turn"),
                         file_change_event(helper, now - 9 * 60 - 29, f.work / "tracked.txt",
                                           turn_id="helper-turn"),
                         assistant_event(helper, now - 1, "Помощник завершил правку")]
        f.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        self.check(f, ["OK"], 0)

    def test_213_edit_between_second_and_third_breaks_series(self):
        f = self.fixture("cycle-edit-between-second-third", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        path = f.work / "tracked.txt"
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                  file_change_event(session, now - 8 * 60 - 30, path, turn_id="leader-turn"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        self.check(f, ["OK"], 0)

    def test_apply_patch_success_breaks_cycle(self):
        f = self.fixture("apply-patch-success-breaks-cycle", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        target = f.work / "patch-target.txt"
        target.write_text("old\n", encoding="utf-8")
        patch_text = f"*** Begin Patch\n*** Update File: {target}\n@@\n-old\n+new\n*** End Patch"
        call_id = "patch-success"
        turn_id = "leader-turn"
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                  custom_exec_call_event(now - 9 * 60 - 30, turn_id, call_id,
                                         "await tools.apply_patch(" + json.dumps(patch_text, ensure_ascii=False) + ")"),
                  file_change_event(session, now - 9 * 60 - 29, target, "update", turn_id, "patch-change"),
                  custom_exec_output_event(now - 9 * 60 - 28, turn_id, call_id, "Синтетический результат."),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        self.check(f, ["OK"], 0)

    def test_failed_patch_does_not_break_cycle(self):
        f = self.fixture("failed-patch-preserves-cycle", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        call_id = "patch-failure"
        turn_id = "leader-turn"
        patch_target = f.work / "missing-patch-target.txt"
        failed_patch = f"*** Begin Patch\n*** Update File: {patch_target}\n@@\n-old\n+new\n*** End Patch"
        events = [command_event(session, now - 10 * 60, "pytest", 1, "", "cmd-1"),
                  custom_exec_call_event(now - 9 * 60 - 30, turn_id, call_id,
                                         "await tools.apply_patch(" + json.dumps(failed_patch, ensure_ascii=False) + ")"),
                  custom_exec_output_event(now - 9 * 60 - 29, turn_id, call_id,
                                           "Синтетическая ошибка patch."),
                  command_event(session, now - 9 * 60, "pytest", 1, "", "cmd-2"),
                  command_event(session, now - 8 * 60, "pytest", 1, "", "cmd-3"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:6"
        self.check(f, [line], 3)

    def test_progress_tool_text_is_not_own_step(self):
        f = self.fixture("progress-tool-text", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        call_id = "tool-progress"
        turn_id = "leader-turn"
        events = [assistant_event(session, now - 121 * 60, "Старый собственный шаг"),
                  custom_exec_call_event(now - 3, turn_id, call_id,
                                         "tools.exec_command({command: 'synthetic progress output'})"),
                  custom_exec_output_event(now - 2, turn_id, call_id, "ХОД: [x] Текст только в tool"),
                  ]
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=events)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_progress_user_text_is_not_own_step(self):
        f = self.fixture("progress-user-text", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        now = f.now
        user_event = event_record("response_item", now - 1, {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "ХОД: [x] Текст только в сообщении user"}],
        })
        events = [assistant_event(session, now - 121 * 60, "Старый собственный шаг"), user_event]
        _, start_time = f.add_run("leader", "main", session, age_minutes=122, live_pid=True, events=events)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3б {session}: застой; непрерывно={AGE} мин; новых строк хода за 120.00 мин=0; источник={rollout}"
        self.check(f, [line], 3, ages=(start_time,))

    def test_218_filechange_outside_frame_signals(self):
        f = self.fixture("filechange-outside-frame", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        outside = f.root / "outside"
        outside.mkdir()
        target = outside / "f"
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_218_filechange_inside_cwd_tmp_and_allowedpath_is_accepted(self):
        f = self.fixture("filechange-inside-allowed-paths", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        allowed_file = f.root / "allowed-file"
        allowed_file.write_text("synthetic input\n", encoding="utf-8")
        allowed_directory = f.root / "allowed-directory"
        allowed_directory.mkdir()
        run, _ = f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        (run / "allowed-paths.txt").write_text(
            str(allowed_file) + "\n" + str(allowed_directory) + "\n", encoding="utf-8")
        now = f.now
        events = [file_change_event(session, now - 90, f.work / "inside.txt"),
                  file_change_event(session, now - 80, Path("/tmp") / (f.root.name + "-file")),
                  file_change_event(session, now - 70, allowed_file, "update"),
                  file_change_event(session, now - 60, allowed_directory / "inside.txt"),
                  assistant_event(session, now - 1, "Свежая проверка")]
        f.write_rollout("main", session, events)
        self.check(f, ["OK"], 0)

    def test_219_symlink_escape_is_outside(self):
        f = self.fixture("filechange-symlink-escape", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        outside = f.root / "outside" / "f"
        outside.parent.mkdir()
        outside.write_text("synthetic target\n", encoding="utf-8")
        link = f.work / "link"
        link.symlink_to(outside)
        events = [file_change_event(session, f.now - 60, link, "update"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(outside), allowed, f.rollouts[session], 2)], 3)

    def test_219_symlink_target_inside_is_allowed(self):
        f = self.fixture("filechange-symlink-inside", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        inside = f.work / "inside"
        inside.mkdir()
        link = f.work / "link"
        link.symlink_to(inside, target_is_directory=True)
        events = [file_change_event(session, f.now - 60, link / "f"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        self.check(f, ["OK"], 0)

    def test_219_cwd_prefix_sibling_is_outside(self):
        f = self.fixture("filechange-cwd-prefix-sibling", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = f.root / "work-other" / "f"
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_219_tmp_prefix_sibling_is_outside(self):
        f = self.fixture("filechange-tmp-prefix-sibling", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = Path("/tmpx/f")
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_219_allowed_file_prefix_sibling_is_outside(self):
        f = self.fixture("filechange-allowed-file-prefix-sibling", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        allowed_file = f.root / "allowed-file"
        allowed_file.write_text("synthetic input\n", encoding="utf-8")
        sibling = f.root / "allowed-file-other"
        sibling.mkdir()
        target = sibling / "f"
        run, _ = f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        (run / "allowed-paths.txt").write_text(str(allowed_file) + "\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        rollout = f.write_rollout("main", session, events)
        allowed = allowed_roots_json([str(f.work), "/tmp", str(allowed_file)])
        self.check(f, [frame_line(session, str(target), allowed, rollout, 2)], 3)

    def test_219_allowed_directory_prefix_sibling_is_outside(self):
        f = self.fixture("filechange-allowed-directory-prefix-sibling", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        allowed_directory = f.root / "allowed-directory"
        allowed_directory.mkdir()
        sibling = f.root / "allowed-directory-other"
        sibling.mkdir()
        target = sibling / "f"
        run, _ = f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        (run / "allowed-paths.txt").write_text(str(allowed_directory) + "\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        rollout = f.write_rollout("main", session, events)
        allowed = allowed_roots_json([str(f.work), "/tmp", str(allowed_directory)])
        self.check(f, [frame_line(session, str(target), allowed, rollout, 2)], 3)

    def test_219_parent_traversal_is_outside(self):
        f = self.fixture("filechange-parent-traversal", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        outside = f.root / "outside"
        outside.mkdir()
        raw_target = f.work / ".." / "outside" / "f"
        target = outside / "f"
        events = [file_change_event(session, f.now - 60, raw_target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_220_helper_out_of_frame_change_signals(self):
        f = self.fixture("helper-filechange-outside-frame", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 5 * 60, "started", helper),
                         assistant_event(leader, now - 1, "Руководитель продолжает")]
        f.add_run("leader", "main", leader, age_minutes=1, live_pid=True, events=parent_events)
        outside = f.root / "outside"
        outside.mkdir()
        target = outside / "f"
        helper_events = [task_boundary_event("task_started", now - 5 * 60, "helper-turn"),
                         file_change_event(helper, now - 60, target, turn_id="helper-turn"),
                         assistant_event(helper, now - 1, "Помощник продолжает")]
        helper_rollout = f.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(helper, str(target), allowed, helper_rollout, 3)], 3)

    def test_220_helper_inside_frame_change_is_allowed(self):
        f = self.fixture("helper-filechange-inside-frame", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 5 * 60, "started", helper),
                         assistant_event(leader, now - 1, "Руководитель продолжает")]
        f.add_run("leader", "main", leader, age_minutes=1, live_pid=True, events=parent_events)
        helper_events = [task_boundary_event("task_started", now - 5 * 60, "helper-turn"),
                         file_change_event(helper, now - 60, f.work / "inside.txt", turn_id="helper-turn"),
                         assistant_event(helper, now - 1, "Помощник продолжает")]
        f.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        self.check(f, ["OK"], 0)

    def test_file_uri_change_is_resolved(self):
        f = self.fixture("filechange-file-uri", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = f.root / "outside" / "f"
        target.parent.mkdir()
        events = [file_change_event(session, f.now - 60, target.as_uri()),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_frame_update_outside_signals(self):
        f = self.fixture("filechange-update-outside", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = f.root / "outside" / "f"
        target.parent.mkdir()
        target.write_text("synthetic input\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, target, "update"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_frame_delete_outside_signals(self):
        f = self.fixture("filechange-delete-outside", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = f.root / "outside" / "f"
        target.parent.mkdir()
        target.write_text("synthetic input\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, target, "delete"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_relative_filechange_uses_run_cwd(self):
        f = self.fixture("filechange-relative-run-cwd", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = f.root / "outside" / "f"
        target.parent.mkdir()
        events = [file_change_event(session, f.now - 60, "../outside/f"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, f.rollouts[session], 2)], 3)

    def test_199_helper_does_not_consume_main_slot(self):
        f = self.fixture("helper-not-main-slot", main_slots=2)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 5 * 60, "started", helper),
                         assistant_event(leader, now - 1, "Руководитель продолжает")]
        f.add_run("leader", "main", leader, age_minutes=1, live_pid=True, events=parent_events)
        helper_events = [task_boundary_event("task_started", now - 5 * 60, "helper-turn"),
                         assistant_event(helper, now - 1, "Помощник продолжает")]
        f.write_rollout("main", helper, helper_events, parent_id=leader, history_start=1)
        self.check(f, [self.split_line("main", 1, 0, f)], 3)

    def test_missing_rate_limits_preserves_independent_capacity(self):
        f = self.fixture("missing-rate-capacity", main_slots=2)
        quota_id = "00000000-0000-4000-8000-000000000001"
        f.rollouts.pop(quota_id).unlink()
        session = "11111111-1111-4111-8111-111111111111"
        f.add_run("leader", "main", session, age_minutes=3, live_pid=True, events=None)
        f.write_rollout("main", session,
                        [rate_event(f.now - 60, None, None),
                         assistant_event(session, f.now - 1, "Свежая проверка")])
        sessions = Path(f.accounts["main"]["codex_home"]) / "sessions"
        self.check(f, [self.pending_line("leader", "rate_limits", sessions),
                       self.split_line("main", 1, 0, f)], 3)

    def test_missing_rollout_signals_and_capacity_independent(self):
        f = self.fixture("missing-rollout-capacity", main_slots=2)
        session = "33333333-3333-4333-8333-333333333333"
        run, start_time = f.add_run("leader", "main", session, age_minutes=7,
                                   live_pid=True, events=None)
        home = Path(f.accounts["main"]["codex_home"])
        source = home / "sessions" / "*" / "*" / "*" / f"rollout-*-{session}.jsonl"
        self.check(f, [self.missing_line(session, "leader", "журнал", source=source),
                       self.split_line("main", 1, 0, f)], 3, ages=(start_time,))

    def test_main_session_id_pending_does_not_block_acct2_capacity(self):
        f = self.fixture("main-session-missing-acct2-capacity", main_slots=1, acct2_slots=1)
        run, _ = f.add_run("leader", "main", None, age_minutes=3, live_pid=True,
                           log_session=False, events=None)
        f.set_state([("ready-task", "", "Готова к запуску")])
        self.check(f, [self.pending_line("leader", "session id", run / "reply.txt.run.log"),
                       self.ready_line("acct2", 1, 1, f)], 3)

    def test_closed_run_without_session_or_rollout_is_ignored(self):
        f = self.fixture("closed-run-missing-session-and-rollout")
        finished = f.now - 60 * 60
        f.add_run("closed", "main", None, age_minutes=90, status=finished, live_pid=False,
                  log_session=False, events=None)
        self.check(f, ["OK"], 0)

    def test_custom_ready_prefix_value_is_used(self):
        f = self.fixture("custom-ready-prefix-value", main_slots=2)
        f.config["plan"]["ready_prefix"] = "Можно запускать"
        f.write_config()
        f.state.write_text(f.state_text([("task-1", "", "Можно запускать задачу"),
                                         ("task-2", "", "Можно запускать задачу"),
                                         ("task-old", "", "Готова к запуску")]), encoding="utf-8")
        self.check(f, [self.ready_line("main", 2, 2, f)], 3)

    def test_custom_closed_prefix_value_is_used(self):
        f = self.fixture("custom-closed-prefix-value")
        f.config["plan"]["closed_prefixes"] = ["Завершено вручную"]
        f.write_config()
        closed_id = "11111111-1111-4111-8111-111111111111"
        open_id = "22222222-2222-4222-8222-222222222222"
        finished = f.now - 17 * 60
        f.add_run("new-prefix-closed", "main", closed_id, age_minutes=30, status=finished,
                  live_pid=False, events=[assistant_event(closed_id, finished - 60, "Завершено")])
        open_run, _ = f.add_run("old-prefix-open", "main", open_id, age_minutes=30, status=finished,
                                live_pid=False, events=[assistant_event(open_id, finished - 60, "Завершено")])
        f.set_state([("closed", closed_id, "Завершено вручную: готово"),
                     ("open", open_id, "Принята задача")])
        status_path = open_run / "status"
        line = f"SIGNAL 2б {open_id}: ожидание; после finished={AGE} мин; порог=15.00 мин; источник={status_path}"
        self.check(f, [line], 3, ages=(finished,))

    def test_custom_task_column_name_is_used(self):
        f = self.fixture("custom-task-column", main_slots=1)
        f.config["plan"]["columns"]["task"] = "Work"
        f.write_config()
        f.state.write_text(f.state_text([("task", "", "Готова к запуску")],
                                       headers=["Work", "Нить", "Его статус"]), encoding="utf-8")
        self.check(f, [self.ready_line("main", 1, 1, f)], 3)

    def test_custom_thread_column_name_is_used(self):
        f = self.fixture("custom-thread-column", main_slots=2)
        session = "11111111-1111-4111-8111-111111111111"
        self.healthy_run(f, session_id=session)
        f.config["plan"]["columns"]["thread"] = "Thread ID"
        f.write_config()
        f.state.write_text(f.state_text([("task", session, "Готова к запуску")],
                                       headers=["Задача", "Thread ID", "Его статус"]), encoding="utf-8")
        self.check(f, [self.split_line("main", 1, 0, f)], 3)

    def test_custom_status_column_name_is_used(self):
        f = self.fixture("custom-status-column", main_slots=1)
        f.config["plan"]["columns"]["status"] = "Phase"
        f.write_config()
        f.state.write_text(f.state_text([("task", "", "Готова к запуску")],
                                       headers=["Задача", "Нить", "Phase"]), encoding="utf-8")
        self.check(f, [self.ready_line("main", 1, 1, f)], 3)

    def test_custom_account_column_name_is_used(self):
        f = self.fixture("custom-account-column", main_slots=1, acct2_slots=1)
        f.config["plan"]["columns"]["account"] = "Scope"
        f.write_config()
        f.state.write_text(f.state_text([("task", "", "Готова к запуску", "acct2")],
                                       headers=["Задача", "Нить", "Его статус", "Scope"]), encoding="utf-8")
        self.check(f, [self.ready_line("acct2", 1, 1, f), self.split_line("main", 1, 0, f)], 3)

    def test_custom_extra_allowed_paths_value_is_used(self):
        f = self.fixture("custom-extra-allowed-paths", main_slots=1)
        extra = f.root / "extra"
        extra.mkdir()
        f.config["extra_allowed_paths"] = [str(extra)]
        f.write_config()
        session = "11111111-1111-4111-8111-111111111111"
        target_tmp = Path("/tmp") / (f.root.name + "-extra") / "file"
        events = [file_change_event(session, f.now - 90, extra / "inside.txt"),
                  file_change_event(session, f.now - 60, target_tmp),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(extra), str(f.work)])
        self.check(f, [frame_line(session, str(target_tmp), allowed, f.rollouts[session], 3)], 3)

    def test_helper_missing_journal_after_grace_signals(self):
        f = self.fixture("helper-missing-journal-after-grace", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        started = f.now - 7 * 60
        self.healthy_run(f, session_id=leader, age_minutes=30)
        f.write_rollout("main", leader, [subagent_event(leader, started, "started", helper),
                                           assistant_event(leader, f.now - 60, "Руководитель продолжает")])
        source = Path(f.accounts["main"]["codex_home"]) / "sessions" / "*" / "*" / "*" / f"rollout-*-{helper}.jsonl"
        self.check(f, [self.missing_line(helper, "leader", "журнал", source=source)],
                   3, ages=(started,))

    def test_interrupted_helper_closes_without_task_complete(self):
        f = self.fixture("interrupted-helper-closes", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 30 * 60, "started", helper),
                         subagent_event(leader, now - 25 * 60, "interrupted", helper),
                         assistant_event(leader, now - 1, "Руководитель продолжает")]
        f.add_run("leader", "main", leader, age_minutes=31, live_pid=True, events=parent_events)
        f.write_rollout("main", helper, [assistant_event(helper, now - 25 * 60, "Ход помощника прерван")],
                        parent_id=leader, history_start=1)
        self.check(f, ["OK"], 0)

    def test_interacted_helper_resume_reopens_interval(self):
        f = self.fixture("interacted-helper-resumes", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        parent_events = [subagent_event(leader, now - 40 * 60, "started", helper),
                         subagent_event(leader, now - 35 * 60, "completed", helper),
                         subagent_event(leader, now - 24 * 60, "interacted", helper),
                         assistant_event(leader, now - 1, "Руководитель продолжает")]
        f.add_run("leader", "main", leader, age_minutes=40, live_pid=True, events=parent_events)
        event_time = now - 22 * 60
        helper_rollout = f.write_rollout(
            "main", helper,
            [task_boundary_event("task_started", now - 24 * 60, "helper-resume"),
             assistant_event(helper, event_time, "Помощник продолжает")],
            parent_id=leader, history_start=1)
        self.check(f, [self.idle_line(helper, helper_rollout, source_line=3)], 3, ages=(event_time,))

    def test_symlinked_cwd_allows_real_path_filechange(self):
        f = self.fixture("filechange-symlinked-cwd", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        alias = f.root / "alias-cwd"
        alias.symlink_to(f.work, target_is_directory=True)
        run, _ = f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        home = f.accounts["main"]["codex_home"]
        (run / "run.sh").write_text(f"cd -- {alias}\nexport CODEX_HOME={home}\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, f.work / "inside.txt"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.write_rollout("main", session, events)
        self.check(f, ["OK"], 0)

    def test_allowed_paths_dotdot_root_is_canonicalized(self):
        f = self.fixture("allowed-paths-dotdot", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        allowed_directory = f.root / "allowed-directory"
        allowed_directory.mkdir()
        (f.root / "x").mkdir()
        raw_allowed = f.root / "x" / ".." / "allowed-directory"
        run, _ = f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        (run / "allowed-paths.txt").write_text(str(raw_allowed) + "\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, allowed_directory / "inside.txt"),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.write_rollout("main", session, events)
        self.check(f, ["OK"], 0)

    def test_allowed_paths_dotdot_root_signals_outside(self):
        f = self.fixture("allowed-paths-dotdot-outside", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        allowed_directory = f.root / "allowed-directory"
        allowed_directory.mkdir()
        (f.root / "x").mkdir()
        raw_allowed = f.root / "x" / ".." / "allowed-directory"
        outside = f.root / "outside"
        outside.mkdir()
        target = outside / "f"
        run, _ = f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        (run / "allowed-paths.txt").write_text(str(raw_allowed) + "\n", encoding="utf-8")
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        rollout = f.write_rollout("main", session, events)
        allowed = allowed_roots_json([str(allowed_directory), str(f.work), "/tmp"])
        self.check(f, [frame_line(session, str(target), allowed, rollout, 2)], 3)

    def test_old_named_active_rollout_is_selected_for_cycle(self):
        f = self.fixture("old-named-active-rollout", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=None)
        events = self.cycle_events(session, count=3, now=f.now)
        rollout = f.write_rollout("main", session, events)
        rollout = f.move_rollout_to_old_named_path(session)
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=pytest; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_old_named_fresh_quota_rollout_is_selected(self):
        f = self.fixture("old-named-quota-rollout")
        snapshot = f.now
        quota_id = "00000000-0000-4000-8000-000000000001"
        f.write_quota("main", primary_used=39, primary_window=300,
                      primary_reset=snapshot + 150 * 60, secondary=False, epoch=snapshot)
        rollout = f.move_rollout_to_old_named_path(quota_id)
        line = f"SIGNAL 1б main: primary: расход=39.00%; ожидается=50.00%; отставание=11.00 п.п.; источник={rollout}:2"
        self.check(f, [line], 3)

    def test_late_null_rate_limits_preserves_latest_nonnull_snapshot(self):
        f = self.fixture("late-null-rate-limits")
        quota_id = "00000000-0000-4000-8000-000000000001"
        snapshot = f.now - 10 * 60
        primary = {"used_percent": 39, "window_minutes": 300, "resets_at": snapshot + 150 * 60}
        rollout = f.write_rollout("main", quota_id,
                                  [rate_event(snapshot, primary, None),
                                   rate_event(f.now - 60, None, None)])
        line = f"SIGNAL 1б main: primary: расход=39.00%; ожидается=50.00%; отставание=11.00 п.п.; источник={rollout}:2"
        self.check(f, [line], 3)

    def test_237_cycle_rollout_mtime_40m_is_not_read(self):
        f = self.fixture("cycle-rollout-mtime-40m", main_slots=1)
        leader_id = "11111111-1111-4111-8111-111111111111"
        unrelated_id = "77777777-7777-4777-8777-777777777777"
        self.healthy_run(f, session_id=leader_id, age_minutes=1)
        events = self.cycle_events(unrelated_id, count=3, now=f.now)
        f.write_rollout("main", unrelated_id, events, mtime=f.now - 40 * 60)
        self.check(f, ["OK"], 0)

    def test_cycle_literal_backslash_is_escaped(self):
        f = self.fixture("cycle-literal-backslash", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        command = "echo 'Привет\\n'"
        events = self.cycle_events(session, count=3, command=command, now=f.now)
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        rollout = f.rollouts[session]
        line = f"SIGNAL 3а {session}: цикл; попыток=3; exit=1; sha256={EMPTY_SHA256}; действие=echo 'Привет\\\\n'; источник={rollout}:4"
        self.check(f, [line], 3)

    def test_frame_newline_and_cyrillic_path_are_escaped(self):
        f = self.fixture("frame-newline-cyrillic-path", main_slots=1)
        session = "11111111-1111-4111-8111-111111111111"
        target = f.root / "outside\nфайл"
        events = [file_change_event(session, f.now - 60, target),
                  assistant_event(session, f.now - 1, "Свежая проверка")]
        f.add_run("leader", "main", session, age_minutes=1, live_pid=True, events=events)
        allowed = allowed_roots_json([str(f.work), "/tmp"])
        escaped_target = str(f.root) + "/outside\\nфайл"
        self.check(f, [frame_line(session, escaped_target, allowed, f.rollouts[session], 2)], 3)

    def test_idle_attributes_fresh_helper_event_to_helper(self):
        f = self.fixture("idle-event-attributed-to-helper", main_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        own_event = now - 22 * 60
        parent_events = [subagent_event(leader, now - 30 * 60, "started", helper),
                         assistant_event(leader, own_event, "Последний собственный шаг руководителя"),
                         command_event(helper, now - 1, "helper-work", 0, "", "helper-event")]
        f.add_run("leader", "main", leader, age_minutes=30, live_pid=True, events=parent_events)
        f.write_rollout("main", helper,
                        [task_boundary_event("task_started", now - 5 * 60, "helper-turn"),
                         assistant_event(helper, now - 1, "Помощник продолжает")],
                        parent_id=leader, history_start=1)
        self.check(f, [self.idle_line(leader, f.rollouts[leader], source_line=3)],
                   3, ages=(own_event,))

    def test_dead_pid_without_session_id_only_orphan_signal(self):
        f = self.fixture("dead-pid-without-session-id")
        run, _ = f.add_run("orphan", "main", None, age_minutes=7, live_pid=False,
                           log_session=False, events=None, pid=42001)
        line = f"SIGNAL 1а main: оборванный прогон; прогон=orphan; pid=42001; источник={run / 'started'}"
        self.check(f, [line], 3, signal_lines=[line])

    def test_state_without_configured_columns_is_error2(self):
        f = self.fixture("state-without-configured-columns")
        f.state.write_text("| Неверная колонка |\n| --- |\n", encoding="utf-8")
        self.check(f, code=2, diagnostic=(f.state, "Задача", "Нить", "Его статус"))

    def test_complete_malformed_jsonl_line_signals_and_independent_checks_continue(self):
        f = self.fixture("complete-malformed-jsonl-line", main_slots=3, acct2_slots=1)
        broken = "11111111-1111-4111-8111-111111111111"
        other = "22222222-2222-4222-8222-222222222222"
        now = f.now
        broken_events = [
            command_event(broken, now - 120, "pytest", 1, "", "broken-attempt-1"),
            command_event(broken, now - 100, "pytest", 1, "", "broken-attempt-2"),
            command_event(broken, now - 80, "pytest", 1, "", "broken-attempt-3"),
            assistant_event(broken, now - 1, "Свежая проверка"),
        ]
        broken_rollout = f.write_rollout("main", broken, broken_events)
        _, broken_start = f.add_run("broken", "main", broken, age_minutes=3, live_pid=True,
                                    events=None, pid=42001)
        with broken_rollout.open("r", encoding="utf-8") as stream:
            records = stream.readlines()
        records.insert(2, '{"payload": broken}\n')
        broken_rollout.write_text("".join(records), encoding="utf-8")

        other_event = now - 22 * 60
        f.add_run("other", "main", other, age_minutes=24, live_pid=True,
                  events=[assistant_event(other, other_event, "Последнее событие")], pid=42002)
        other_rollout = f.rollouts[other]
        f.config["plan"]["columns"]["account"] = "Аккаунт"
        f.write_config()
        f.set_state([("ready-acct2", "", "Готова к запуску", "acct2")], account_column=True)

        lines = [
            self.ready_line("acct2", 1, 1, f),
            self.idle_line(other, other_rollout, source_line=2),
            self.split_line("main", 1, 0, f),
            f"SIGNAL 5 {broken}: нет данных; прогон=broken; журнал неразбираем, строка 3; возраст={AGE} мин; источник={broken_rollout}",
        ]
        self.check(f, lines, 3, ages=(other_event, broken_start))

    def test_mixed_helper_history_without_boundary_signals_and_independent_checks_continue(self):
        f = self.fixture("mixed-helper-history-without-boundary", main_slots=2, acct2_slots=1)
        leader = "11111111-1111-4111-8111-111111111111"
        helper = "33333333-3333-4333-8333-333333333333"
        now = f.now
        leader_events = [subagent_event(leader, now - 170, "started", helper)]
        leader_events.extend(
            command_event(leader, now - 120 + 2 * index, f"leader-work-{index}", 0, "",
                          f"leader-command-{index}")
            for index in range(21)
        )
        leader_events.append(assistant_event(leader, now - 1, "Руководитель продолжает"))
        _, leader_start = f.add_run("leader", "main", leader, age_minutes=3, live_pid=True,
                                    events=leader_events, pid=42001)
        helper_events = [assistant_event(helper, now - 25 * 60, "Унаследованная история"),
                         assistant_event(helper, now - 22 * 60, "Продолжение без доказуемой границы")]
        helper_rollout = f.write_rollout("main", helper, helper_events, parent_id=leader, history_start=None)

        f.config["plan"]["columns"]["account"] = "Аккаунт"
        f.write_config()
        f.set_state([("ready-acct2", "", "Готова к запуску", "acct2")], account_column=True)
        lines = [
            self.ready_line("acct2", 1, 1, f),
            self.split_line("main", 1, 0, f),
            f"SIGNAL 5 {helper}: нет данных; прогон=leader; история помощника без границы; возраст={AGE} мин; источник={helper_rollout}",
        ]
        self.check(f, lines, 3, ages=(leader_start,))

