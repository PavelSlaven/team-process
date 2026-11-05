#!/usr/bin/env python3
import argparse
import collections
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
import re
import shlex
import signal
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

MAX_RUNTIME = 60
UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def norm(command):
    command = re.sub(r"\s+", " ", str(command))
    marker = command.find("'-Command', ")
    return command[marker + 12:] if marker >= 0 else command


def strict_norm(command):
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command]
        shell = os.path.basename(parts[0]).lower() if parts else ""
        if shell in {"sh", "bash", "dash", "zsh", "ksh", "fish", "pwsh", "powershell", "powershell.exe"}:
            for index, option in enumerate(parts[:-1]):
                if index and option.startswith("-") and "c" in option[1:] and not option.startswith("--"):
                    parts[index + 1] = strict_norm(parts[index + 1])
                    break
        command = shlex.join(parts)
    command = str(command)
    out = []
    quote = None
    escaped = False
    pending_space = False
    i = 0
    while i < len(command):
        char = command[i]
        if quote:
            if pending_space:
                out.append(" ")
                pending_space = False
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            if pending_space and out:
                out.append(" ")
            pending_space = False
            quote = char
            out.append(char)
        elif char in (" ", "\t"):
            pending_space = True
        elif char == "#" and (not out or pending_space):
            while i < len(command) and command[i] != "\n":
                i += 1
            if i < len(command):
                out.append("\n")
                pending_space = False
        else:
            if pending_space and out and out[-1] != "\n":
                out.append(" ")
            pending_space = False
            out.append(char)
        i += 1
    return "".join(out).strip(" \t")


def new_counter():
    return collections.Counter()


def record_attempt(counter, command, exit_code=None, output=None, strict=False):
    if strict:
        digest = hashlib.sha256(str(output or "").encode("utf-8")).hexdigest()
        key = (strict_norm(command), exit_code, digest)
    else:
        key = norm(command)
    counter[key] += 1
    return key, counter[key]


class InputError(Exception):
    pass


class InputTimeout(InputError):
    pass


def _alarm_handler(_signum, _frame):
    raise InputTimeout("budget expired")


class Checker:
    def __init__(self, config_path):
        self.started = time.monotonic()
        self.now = time.time()
        self.config_path = Path(config_path)
        self.current_path = str(self.config_path)
        self.limit = MAX_RUNTIME
        self.deadline = self.started + MAX_RUNTIME
        self.timer_active = False
        self.signals = set()
        self.pending = set()
        self.accounts = {}
        self.runs = []
        self.plan = None
        self.files = {}
        self.journals = collections.defaultdict(list)
        self.rates = collections.defaultdict(list)
        self.bad_journals = {}
        self.helper_unknown = set()
        self.helper_missing = {}
        self.account_session_unknown = set()
        self.account_plan_unknown = False

    def tick(self, path=None):
        if path is not None:
            self.current_path = str(path)
        if time.monotonic() >= self.deadline:
            raise InputTimeout("budget expired")

    @contextmanager
    def timed(self, path):
        self.tick(path)
        try:
            yield
        except InputTimeout:
            self.current_path = str(path)
            raise

    def arm_timer(self):
        signal.setitimer(signal.ITIMER_REAL, max(0.001, self.deadline - time.monotonic()))

    @contextmanager
    def overall_timer(self):
        old_handler = signal.getsignal(signal.SIGALRM)
        old_remaining, old_interval = signal.getitimer(signal.ITIMER_REAL)
        timer_started = time.monotonic()
        signal.signal(signal.SIGALRM, _alarm_handler)
        self.timer_active = True
        self.arm_timer()
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            self.timer_active = False
            signal.signal(signal.SIGALRM, old_handler)
            if old_remaining:
                elapsed = time.monotonic() - timer_started
                signal.setitimer(signal.ITIMER_REAL, max(0.001, old_remaining - elapsed), old_interval)

    def read_text(self, path):
        path = Path(path)
        with self.timed(path):
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise InputError(f"{path}: {exc}") from None

    @staticmethod
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    def load_config(self):
        with self.timed(self.config_path):
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"),
                                 object_pairs_hook=self.unique_object)
            except InputTimeout:
                raise
            except (OSError, UnicodeError, ValueError) as exc:
                raise InputError(f"{self.config_path}: {exc}") from None
        self.validate_config(raw)

    def validate_config(self, raw):
        top = {"tasks_dir", "proc_dir", "accounts", "plan", "extra_allowed_paths",
               "report_path", "signal_path", "thresholds"}
        if not isinstance(raw, dict):
            raise InputError(f"{self.config_path}: configuration must be an object")
        self.check_keys(raw, top, "configuration")
        self.config = raw
        for key in ("tasks_dir", "proc_dir", "report_path", "signal_path"):
            self.path_value(raw[key], f"{self.config_path}:{key}")
        accounts = raw["accounts"]
        if not isinstance(accounts, dict) or not accounts:
            raise InputError(f"{self.config_path}: accounts must be a nonempty object")
        roots = set()
        self.accounts = {}
        for name, value in accounts.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                raise InputError(f"{self.config_path}: invalid account")
            self.check_keys(value, {"codex_home", "slots", "helper_limit", "plan_deadline"},
                            f"accounts.{name}")
            home = self.path_value(value["codex_home"], f"{self.config_path}:accounts.{name}.codex_home")
            root = os.path.realpath(home)
            if root in roots:
                raise InputError(f"{self.config_path}: duplicate account root {home}")
            roots.add(root)
            slots = self.integer(value["slots"], f"accounts.{name}.slots", minimum=0)
            helper_limit = self.integer(value["helper_limit"], f"accounts.{name}.helper_limit", minimum=0)
            deadline = value["plan_deadline"]
            if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                                         or not math.isfinite(deadline)):
                raise InputError(f"{self.config_path}: invalid plan_deadline for {name}")
            self.accounts[name] = dict(name=name, home=home, root=root, slots=slots,
                                       helper_limit=helper_limit, plan_deadline=deadline)
        plan = raw["plan"]
        if not isinstance(plan, dict):
            raise InputError(f"{self.config_path}: plan must be an object")
        self.check_keys(plan, {"path", "columns", "ready_prefix", "closed_prefixes"}, "plan")
        self.path_value(plan["path"], f"{self.config_path}:plan.path")
        columns = plan["columns"]
        if not isinstance(columns, dict):
            raise InputError(f"{self.config_path}: plan.columns must be an object")
        self.check_keys(columns, {"task", "thread", "status", "account"}, "plan.columns")
        for key in ("task", "thread", "status"):
            if not isinstance(columns[key], str) or not columns[key]:
                raise InputError(f"{self.config_path}: invalid plan.columns.{key}")
        if columns["account"] is not None and (not isinstance(columns["account"], str) or not columns["account"]):
            raise InputError(f"{self.config_path}: invalid plan.columns.account")
        if not isinstance(plan["ready_prefix"], str) or not plan["ready_prefix"]:
            raise InputError(f"{self.config_path}: invalid plan.ready_prefix")
        if not isinstance(plan["closed_prefixes"], list) or any(type(item) is not str for item in plan["closed_prefixes"]):
            raise InputError(f"{self.config_path}: invalid plan.closed_prefixes")
        extra = raw["extra_allowed_paths"]
        if not isinstance(extra, list):
            raise InputError(f"{self.config_path}: invalid extra_allowed_paths")
        raw["extra_allowed_paths"] = [self.path_value(item, f"{self.config_path}:extra_allowed_paths")
                                      for item in extra]
        thresholds = raw["thresholds"]
        threshold_keys = {"window_minutes", "rate_snapshot_hours", "underuse_pp", "idle_minutes",
                          "waiting_minutes", "repeat_count", "stagnation_minutes", "data_grace_minutes",
                          "leader_command_count", "runtime_seconds"}
        if not isinstance(thresholds, dict):
            raise InputError(f"{self.config_path}: thresholds must be an object")
        self.check_keys(thresholds, threshold_keys, "thresholds")
        for key in ("window_minutes", "rate_snapshot_hours", "idle_minutes", "waiting_minutes",
                    "stagnation_minutes", "data_grace_minutes", "runtime_seconds"):
            self.number(thresholds[key], f"thresholds.{key}", positive=True)
        self.number(thresholds["underuse_pp"], "thresholds.underuse_pp", positive=False)
        for key in ("repeat_count", "leader_command_count"):
            self.integer(thresholds[key], f"thresholds.{key}", minimum=0 if key == "leader_command_count" else 1)
        if thresholds["runtime_seconds"] > MAX_RUNTIME:
            raise InputError(f"{self.config_path}: thresholds.runtime_seconds exceeds {MAX_RUNTIME}")
        self.limit = float(thresholds["runtime_seconds"])
        self.deadline = self.started + self.limit
        self.tick(self.config_path)
        if self.timer_active:
            self.arm_timer()

    def check_keys(self, value, expected, label):
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        if missing or unknown:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unknown:
                details.append("unknown " + ", ".join(unknown))
            raise InputError(f"{self.config_path}: {label}: {'; '.join(details)}")

    def path_value(self, value, label):
        if not isinstance(value, str) or not os.path.isabs(value):
            raise InputError(f"{self.config_path}: {label} must be an absolute path")
        return value

    def integer(self, value, label, minimum):
        if type(value) is not int or value < minimum:
            raise InputError(f"{self.config_path}: invalid {label}")
        return value

    def number(self, value, label, positive):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InputError(f"{self.config_path}: invalid {label}")
        if value <= 0 if positive else value < 0:
            raise InputError(f"{self.config_path}: invalid {label}")
        return float(value)

    def add_signal(self, line):
        self.signals.add(line)

    def add_pending(self, run_id, kind, source):
        self.pending.add(f"PENDING {escape(run_id)}: {kind}; источник={escape(str(source))}")

    def source_missing(self, run, kind, source, thread_id=None, age_start=None):
        start = run["started"] if age_start is None else age_start
        age = (self.now - start) / 60
        if age < self.config["thresholds"]["data_grace_minutes"]:
            self.add_pending(run["id"], kind, source)
        else:
            ident = thread_id or run["account"]
            self.add_signal(f"SIGNAL 5 {escape(ident)}: нет данных; прогон={escape(run['id'])}; {kind}; "
                            f"возраст={age:.2f} мин; источник={escape(str(source))}")

    def scan_runs(self):
        root = Path(self.config["tasks_dir"])
        if not root.is_dir():
            raise InputError(f"{root}: cannot read configured tasks directory")
        try:
            entries = sorted(os.scandir(root), key=lambda entry: entry.name)
        except OSError as exc:
            raise InputError(f"{root}: {exc}") from None
        for entry in entries:
            self.tick(root)
            if not entry.is_dir(follow_symlinks=False):
                continue
            folder = Path(entry.path)
            started_path = folder / "started"
            status_path = folder / "status"
            if not started_path.exists():
                continue
            text = self.read_text(started_path)
            start_match = re.search(r"(?m)^([^\s]+)", text)
            if not start_match:
                raise InputError(f"{started_path}: missing timestamp")
            started = parse_epoch(start_match.group(1), started_path, self.config_path)
            if status_path.exists():
                status = self.read_text(status_path)
                finished_match = re.search(r"(?m)^finished=([^\s]+)", status)
                if not finished_match:
                    raise InputError(f"{status_path}: missing finished timestamp")
                finished = parse_epoch(finished_match.group(1), status_path, self.config_path)
                run = dict(id=folder.name, folder=folder, started_path=started_path,
                           status_path=status_path, started=started, pid=None, account=None,
                           account_data=None, cwd=None, status=status, finished=finished,
                           alive=False, session=None, session_path=folder / "reply.txt.run.log",
                           allowed=None, orphan=False, active=False)
                run["session"] = self.session_id(run)
                if run["session"]:
                    self.runs.append(run)
                continue
            pid_match = re.search(r"\bpid=(\d+)\b", text)
            if not pid_match:
                raise InputError(f"{started_path}: missing pid")
            pid = int(pid_match.group(1))
            shell_path = folder / "run.sh"
            account_name, cwd = self.run_settings(shell_path)
            account = self.accounts[account_name]
            alive = self.proc_alive(pid)
            run = dict(id=folder.name, folder=folder, started_path=started_path, status_path=status_path,
                       started=started, pid=pid, account=account_name, account_data=account,
                       cwd=cwd, status=None, finished=None, alive=alive, session=None,
                       session_path=folder / "reply.txt.run.log", allowed=None,
                       orphan=not alive, active=alive)
            run["session"] = self.session_id(run)
            self.runs.append(run)
            if run["orphan"]:
                self.add_signal(f"SIGNAL 1а {account_name}: оборванный прогон; прогон={escape(run['id'])}; "
                                f"pid={pid}; источник={escape(str(started_path))}")
            if run["active"] and run["session"] is None:
                self.account_session_unknown.add(account_name)
                self.source_missing(run, "session id", run["session_path"])

    def run_settings(self, path):
        text = self.read_text(path)
        home = None
        cwd = None
        for line in text.splitlines():
            try:
                parts = shlex.split(line, posix=True)
            except ValueError:
                continue
            if len(parts) >= 3 and parts[:2] == ["cd", "--"]:
                cwd = os.path.realpath(parts[2])
            if parts and parts[0] == "export":
                for assignment in parts[1:]:
                    if assignment.startswith("CODEX_HOME="):
                        home = assignment.split("=", 1)[1]
        if not home or not os.path.isabs(home) or cwd is None:
            raise InputError(f"{path}: missing absolute CODEX_HOME or cd --")
        matched = [name for name, data in self.accounts.items() if os.path.realpath(home) == data["root"]]
        if len(matched) != 1:
            raise InputError(f"{path}: CODEX_HOME is not a configured account")
        return matched[0], cwd

    def session_id(self, run):
        path = run["session_path"]
        if not path.exists():
            return None
        text = self.read_text(path)
        match = re.search(r"(?m)^session id:\s*([0-9a-fA-F-]+)\s*$", text)
        if not match:
            return None
        try:
            import uuid
            return str(uuid.UUID(match.group(1)))
        except ValueError:
            return None

    def proc_alive(self, pid):
        return (Path(self.config["proc_dir"]) / str(pid)).exists()

    def descendants_alive(self, pid):
        proc = Path(self.config["proc_dir"])
        todo = [str(pid)]
        seen = set(todo)
        while todo:
            current = todo.pop()
            task_dir = proc / current / "task"
            if not task_dir.is_dir():
                continue
            try:
                tids = [entry for entry in os.scandir(task_dir) if entry.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for task in tids:
                children = Path(task.path) / "children"
                if not children.exists():
                    continue
                text = self.read_text(children)
                for child in text.split():
                    if child not in seen and (proc / child).exists():
                        return True
                    if child not in seen:
                        seen.add(child)
                        todo.append(child)
        return False

    def sessions_inventory(self):
        for account, data in self.accounts.items():
            root = Path(data["home"]) / "sessions"
            if not root.exists():
                self.files[account] = collections.defaultdict(list)
                continue
            if not root.is_dir():
                raise InputError(f"{root}: not a directory")
            found = collections.defaultdict(list)
            try:
                years = list(os.scandir(root))
            except OSError as exc:
                raise InputError(f"{root}: {exc}") from None
            for year in years:
                self.tick(root)
                if not year.is_dir(follow_symlinks=False):
                    continue
                try:
                    months = list(os.scandir(year.path))
                except OSError as exc:
                    raise InputError(f"{year.path}: {exc}") from None
                for month in months:
                    self.tick(month.path)
                    if not month.is_dir(follow_symlinks=False):
                        continue
                    try:
                        days = list(os.scandir(month.path))
                    except OSError as exc:
                        raise InputError(f"{month.path}: {exc}") from None
                    for day in days:
                        self.tick(day.path)
                        if not day.is_dir(follow_symlinks=False):
                            continue
                        try:
                            files = list(os.scandir(day.path))
                        except OSError as exc:
                            raise InputError(f"{day.path}: {exc}") from None
                        for item in files:
                            if item.is_symlink() or not item.is_file(follow_symlinks=False) or not item.name.endswith(".jsonl"):
                                continue
                            try:
                                mtime = item.stat(follow_symlinks=False).st_mtime
                            except OSError as exc:
                                raise InputError(f"{item.path}: {exc}") from None
                            match = UUID_RE.search(item.name[:-6])
                            thread_id = match.group(1).lower() if match else None
                            found[thread_id].append((Path(item.path), mtime))
            self.files[account] = found

    def parse_journal(self, account, path, mtime):
        thread_hint = self.filename_id(path)
        if any(row["path"] == path for row in self.journals.get(thread_hint, [])):
            return
        rows = []
        meta_id = None
        parent = None
        history_start = None
        ordinal = 0
        bad_line = None
        rate_rows = []
        try:
            with self.timed(path):
                with path.open("r", encoding="utf-8") as stream:
                    for line_no, line in enumerate(stream, 1):
                        self.tick(path)
                        if not line.endswith("\n"):
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            bad_line = line_no
                            break
                        if not isinstance(record, dict):
                            continue
                        if record.get("type") == "session_meta":
                            payload = record.get("payload") or {}
                            if isinstance(payload, dict):
                                raw_id = payload.get("id")
                                if isinstance(raw_id, str):
                                    meta_id = raw_id.lower()
                                raw_parent = payload.get("parent_thread_id")
                                source = payload.get("source") or {}
                                if not raw_parent and isinstance(source, dict):
                                    raw_parent = ((source.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id")
                                parent = raw_parent.lower() if isinstance(raw_parent, str) else None
                                history_start = payload.get("subagent_history_start_ordinal")
                            continue
                        ordinal += 1
                        timestamp = record.get("timestamp")
                        if not timestamp:
                            continue
                        try:
                            epoch = parse_epoch(timestamp, path, self.config_path)
                        except InputError:
                            bad_line = line_no
                            break
                        if epoch > self.now:
                            continue
                        payload = record.get("payload") or {}
                        if not isinstance(payload, dict):
                            continue
                        ptype = payload.get("type")
                        if ptype == "token_count":
                            limits = payload.get("rate_limits")
                            if isinstance(limits, dict) and any(limits.get(key) is not None for key in ("primary", "secondary")):
                                rate_rows.append((epoch, path, line_no, limits, mtime))
                        item = payload.get("item") or {}
                        if not isinstance(item, dict):
                            item = {}
                        raw_owner = payload.get("thread_id") or item.get("thread_id") or record.get("thread_id")
                        owner = raw_owner.lower() if isinstance(raw_owner, str) else None
                        kind = None
                        if item.get("type") == "SubAgentActivity":
                            kind = "subagent"
                        elif ptype in ("task_started", "task_complete", "turn_aborted"):
                            kind = "task_boundary"
                        elif item.get("type") == "CommandExecution":
                            kind = "command"
                        elif item.get("type") == "FileChange":
                            kind = "filechange"
                        elif ptype == "message":
                            kind = "message"
                        elif record.get("type") == "response_item" and ptype in ("custom_tool_call", "custom_tool_call_output"):
                            kind = "custom_call" if ptype == "custom_tool_call" else "custom_output"
                        event = dict(epoch=epoch, path=path, line=line_no, mtime=mtime,
                                     ordinal=record.get("ordinal", ordinal), explicit_owner=owner,
                                     kind=kind, event_type=ptype, item=item)
                        if kind == "subagent":
                            event["agent_id"] = item.get("agent_thread_id")
                            event["activity"] = item.get("kind")
                        elif kind == "task_boundary":
                            event["boundary"] = ptype
                            event["turn_id"] = payload.get("turn_id")
                        elif kind == "command":
                            event["command"] = item.get("command")
                            event["status"] = item.get("status")
                            event["exit"] = item.get("exit_code")
                            event["output"] = item.get("aggregated_output") or ""
                            event["item_id"] = item.get("id")
                            event["cwd"] = item.get("cwd")
                            event["process_id"] = item.get("process_id")
                        elif kind == "filechange":
                            event["change_status"] = item.get("status")
                            event["changes"] = item.get("changes") or {}
                            event["turn_id"] = payload.get("turn_id")
                        elif kind == "message":
                            event["role"] = payload.get("role")
                            if payload.get("role") == "assistant":
                                parts = [content["text"] for content in payload.get("content") or []
                                         if isinstance(content, dict) and isinstance(content.get("text"), str)]
                                event["progress"] = progress_lines("\n".join(parts))
                        elif kind in ("custom_call", "custom_output"):
                            event["call_id"] = payload.get("call_id")
                            event["input"] = payload.get("input")
                            event["tool_name"] = payload.get("name")
                            passthrough = payload.get("internal_chat_message_metadata_passthrough") or {}
                            event["turn_id"] = payload.get("turn_id") or (
                                passthrough.get("turn_id") if isinstance(passthrough, dict) else None)
                            event["session_id"] = payload.get("session_id")
                            event["process_id"] = payload.get("process_id")
                            output = payload.get("output")
                            if isinstance(output, list):
                                event["output_text"] = "\n".join(
                                    part.get("text", "") for part in output if isinstance(part, dict))
                            else:
                                event["output_text"] = str(output or "")
                        rows.append(event)
        except InputTimeout:
            raise
        except (OSError, UnicodeError) as exc:
            raise InputError(f"{path}: {exc}") from None
        if meta_id and thread_hint and meta_id != thread_hint:
            bad_line = bad_line or 1
        if history_start is None and parent:
            candidates = [row["ordinal"] for row in rows if row["explicit_owner"] == meta_id]
            candidates.extend(row["ordinal"] for row in rows
                             if row["kind"] == "task_boundary" and row["boundary"] == "task_started")
            boundary = min(candidates) if candidates else None
        else:
            boundary = history_start
        boundary_proven = not parent or history_start is not None or boundary is not None
        for row in rows:
            row["owner"] = row["explicit_owner"]
            row["inherited_prefix"] = bool(parent and (boundary is None or row["ordinal"] < boundary))
            if (not row["inherited_prefix"] and row["owner"] is None and meta_id and
                    boundary is not None and row["ordinal"] >= boundary):
                row["owner"] = meta_id
            elif not row["inherited_prefix"] and row["owner"] is None and meta_id and not parent:
                row["owner"] = meta_id
        journal = dict(account=account, path=path, mtime=mtime, thread=thread_hint or meta_id,
                       parent=parent, history_start=history_start, boundary=boundary,
                       boundary_proven=boundary_proven, bad_line=bad_line, events=rows)
        self.journals[journal["thread"]].append(journal)
        if bad_line is None:
            for epoch, rate_path, line_no, limits, rate_mtime in rate_rows:
                self.rates[account].append((epoch, rate_path, line_no, limits, rate_mtime))

    @staticmethod
    def filename_id(path):
        match = UUID_RE.search(Path(path).name[:-6])
        return match.group(1).lower() if match else None

    def load_journals(self):
        horizon = max(self.config["thresholds"]["rate_snapshot_hours"] * 3600,
                      self.config["thresholds"]["stagnation_minutes"] * 60,
                      self.config["thresholds"]["window_minutes"] * 60)
        lower = self.now - horizon
        for account, files in self.files.items():
            for items in files.values():
                for path, mtime in items:
                    if lower <= mtime <= self.now:
                        self.tick(path)
                        self.parse_journal(account, path, mtime)
        queue = collections.deque((run["account"], run["session"].lower()) for run in self.runs
                                  if run["active"] and run["session"])
        seen = set()
        while queue:
            account, thread_id = queue.popleft()
            key = (account, thread_id)
            if key in seen:
                continue
            seen.add(key)
            for path, mtime in self.files.get(account, {}).get(thread_id, ()):
                self.tick(path)
                self.parse_journal(account, path, mtime)
            related = list(self.journals.get(thread_id, ()))
            states = {}
            subevents = []
            for journal in related:
                if journal["account"] != account or journal["bad_line"] is not None:
                    continue
                subevents.extend(event for event in journal["events"]
                                 if not event["inherited_prefix"] and event["kind"] == "subagent" and
                                 event["owner"] == thread_id)
            subevents.sort(key=lambda event: (event["epoch"], str(event["path"]), event["line"]))
            for event in subevents:
                child = event.get("agent_id")
                if not isinstance(child, str):
                    continue
                child = child.lower()
                if event.get("activity") in ("started", "interacted"):
                    states[child] = True
                elif event.get("activity") in ("completed", "interrupted"):
                    states[child] = False
            for child, active in states.items():
                if active:
                    queue.append((account, child))
        reported_bad = set()
        for run in self.runs:
            if not run["active"] or not run["session"]:
                continue
            thread_id = run["session"].lower()
            matching = [journal for journal in self.journals.get(thread_id, [])
                        if journal["account"] == run["account"]]
            if not matching:
                self.source_missing(run, "журнал", self.rollout_glob(run["account"], thread_id),
                                    thread_id=thread_id)
                continue
            bad = [journal for journal in matching if journal["bad_line"] is not None]
            if bad and thread_id not in reported_bad:
                journal = sorted(bad, key=lambda row: (str(row["path"]), row["bad_line"]))[0]
                self.add_signal(
                    f"SIGNAL 5 {thread_id}: нет данных; прогон={escape(run['id'])}; "
                    f"журнал неразбираем, строка {journal['bad_line']}; "
                    f"возраст={(self.now - run['started']) / 60:.2f} мин; источник={escape(str(journal['path']))}")
                reported_bad.add(thread_id)
            elif not bad and not self.all_events(thread_id, run["account"]):
                self.source_missing(run, "журнал", self.rollout_glob(run["account"], thread_id),
                                    thread_id=thread_id)

    def rollout_glob(self, account, thread_id):
        root = Path(self.accounts[account]["home"]) / "sessions"
        return root / "*" / "*" / "*" / f"rollout-*-{thread_id}.jsonl"

    def read_plan(self):
        path = Path(self.config["plan"]["path"])
        self.tick(path)
        if not path.exists():
            for run in self.runs:
                if run["active"]:
                    self.account_plan_unknown = True
                    self.source_missing(run, "источник плана", path, thread_id=run["session"])
            return None
        with self.timed(path):
            return self.parse_plan(self.read_text(path), path)

    def parse_plan(self, text, path):
        config = self.config["plan"]
        columns = config["columns"]
        required = [columns[key] for key in ("task", "thread", "status")]
        if columns["account"] is not None:
            required.append(columns["account"])
        rows = []
        found = None
        lines = text.splitlines()
        for index, line in enumerate(lines[:-1]):
            if not line.strip().startswith("|") or not lines[index + 1].strip().startswith("|"):
                continue
            header = split_table_row(line)
            if not all(name in header for name in required):
                continue
            if not re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1]):
                continue
            found = {name: header.index(name) for name in required}
            for row_line in lines[index + 2:]:
                if not row_line.strip().startswith("|"):
                    break
                cells = split_table_row(row_line)
                if len(cells) >= len(header):
                    rows.append(cells)
            break
        if found is None:
            missing = [name for name in required
                       if not any(name in split_table_row(line) for line in lines if line.strip().startswith("|"))]
            if not missing:
                missing = required
            raise InputError(f"{path}: missing configured columns: {', '.join(missing)}")
        return dict(path=path, rows=rows, index=found)

    def ready_counts(self, plan):
        ready = collections.defaultdict(set)
        if plan is None:
            return ready
        indexes = plan["index"]
        config = self.config["plan"]
        live = {run["session"].lower() for run in self.runs
                if run["active"] and run["session"]}
        all_accounts = set(self.accounts)
        for cells in plan["rows"]:
            task = cells[indexes[config["columns"]["task"]]].strip()
            thread = cells[indexes[config["columns"]["thread"]]].strip().lower()
            status = cells[indexes[config["columns"]["status"]]].strip()
            if not task or not status.startswith(config["ready_prefix"]) or (thread and thread in live):
                continue
            if config["columns"]["account"] is None:
                selected = all_accounts
            else:
                value = cells[indexes[config["columns"]["account"]]].strip()
                selected = all_accounts if not value else {
                    part.strip() for part in value.split(",") if part.strip()}
                unknown = selected - all_accounts
                if unknown:
                    raise InputError(f"{plan['path']}: unknown account in plan: {', '.join(sorted(unknown))}")
            for account in selected:
                ready[account].add(task)
        return ready

    def capacity_checks(self, ready, plan):
        for name, account in self.accounts.items():
            if name in self.account_session_unknown or self.account_plan_unknown or plan is None:
                continue
            active = {run["session"].lower() for run in self.runs
                      if run["active"] and run["account"] == name and run["session"]}
            free = account["slots"] - len(active)
            count = len(ready.get(name, ()))
            if free > 0 and count > 0:
                self.add_signal(f"SIGNAL 1в {name}: есть работа и места: запусти; свободно={free}; "
                                f"готово={count}; источник={escape(str(plan['path']))}")
            if free > 0 and count < free:
                self.add_signal(f"SIGNAL 4а {name}: дроби план; свободно={free}; готово={count}; "
                                f"источник={escape(str(plan['path']))}")

    def rate_checks(self):
        limit_hours = float(self.config["thresholds"]["rate_snapshot_hours"])
        age_limit = limit_hours * 3600
        underuse = float(self.config["thresholds"]["underuse_pp"])
        active_by_account = collections.defaultdict(list)
        for run in self.runs:
            if run["active"]:
                active_by_account[run["account"]].append(run)
        for name, account in self.accounts.items():
            rows = [row for row in self.rates[name]
                    if row[0] <= self.now and self.now - age_limit <= row[4] <= self.now]
            latest = max(rows, key=lambda row: row[0]) if rows else None
            valid = latest is not None and self.now - latest[0] <= age_limit
            limits = latest[3] if valid else None
            windows = []
            if isinstance(limits, dict):
                for label in ("primary", "secondary"):
                    value = limits.get(label)
                    if value is None or not isinstance(value, dict):
                        continue
                    used = value.get("used_percent")
                    minutes = value.get("window_minutes")
                    resets = value.get("resets_at")
                    if (isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used)
                            or used < 0 or used > 100
                            or isinstance(minutes, bool) or not isinstance(minutes, (int, float))
                            or not math.isfinite(minutes) or minutes <= 0
                            or isinstance(resets, bool) or not isinstance(resets, (int, float))
                            or not math.isfinite(resets)):
                        continue
                    if resets < self.now:
                        continue
                    start = resets - minutes * 60
                    end = resets
                    deadline = account["plan_deadline"]
                    if deadline is not None and deadline < end:
                        if deadline <= start:
                            raise InputError(f"{self.config_path}: plan_deadline for {name} is not after window start")
                        end = deadline
                    expected = 100 * min(1.0, max(0.0, (latest[0] - start) / (end - start)))
                    windows.append((label, float(used), expected, expected - float(used)))
            if not valid or not windows:
                missing = [run for run in active_by_account[name]
                           if not valid]
                waiting = False
                for run in missing:
                    self.source_missing(run, "rate_limits", Path(account["home"]) / "sessions",
                                        thread_id=run["session"])
                    waiting = waiting or ((self.now - run["started"]) / 60 <
                                          self.config["thresholds"]["data_grace_minutes"])
                if waiting:
                    continue
                sessions = Path(account["home"]) / "sessions"
                self.add_signal(f"SIGNAL 1б {name}: нет свежего снимка; предел={limit_hours:.2f} ч; "
                                f"источник={escape(str(sessions))}")
                continue
            for label, used, expected, gap in windows:
                if gap > underuse:
                    self.add_signal(f"SIGNAL 1б {name}: {label}: расход={used:.2f}%; ожидается={expected:.2f}%; "
                                    f"отставание={gap:.2f} п.п.; источник={escape(str(latest[1]))}:{latest[2]}")

    def plan_waiting_checks(self, plan):
        if plan is None:
            return
        config = self.config["plan"]
        indexes = plan["index"]
        closed = config["closed_prefixes"]
        waiting = float(self.config["thresholds"]["waiting_minutes"])
        for cells in plan["rows"]:
            thread_id = cells[indexes[config["columns"]["thread"]]].strip().lower()
            status = cells[indexes[config["columns"]["status"]]].strip()
            if not thread_id or any(status.startswith(prefix) for prefix in closed):
                continue
            matching = [run for run in self.runs if run["session"] and run["session"].lower() == thread_id]
            if not matching:
                continue
            latest = max(matching, key=lambda run: run["started"])
            if latest["active"] or latest["orphan"] or latest["finished"] is None:
                continue
            age = (self.now - latest["finished"]) / 60
            if age > waiting:
                self.add_signal(f"SIGNAL 2б {thread_id}: ожидание; после finished={age:.2f} мин; "
                                f"порог={waiting:.2f} мин; источник={escape(str(latest['status_path']))}")

    def run_allowed(self, run):
        if run["allowed"] is not None:
            return run["allowed"]
        roots = [run["cwd"], *self.config["extra_allowed_paths"]]
        path = run["folder"] / "allowed-paths.txt"
        if path.exists():
            for line in self.read_text(path).splitlines():
                candidate = line.strip()
                if candidate:
                    if not os.path.isabs(candidate):
                        raise InputError(f"{path}: allowed paths must be absolute")
                    roots.append(candidate)
        run["allowed"] = [canonical_nonexistent(candidate) for candidate in roots]
        return run["allowed"]

    def frame_checks(self, thread_id, events, run):
        allowed = self.run_allowed(run)
        lower = self.now - float(self.config["thresholds"]["window_minutes"]) * 60
        patch_paths_by_turn = collections.defaultdict(list)
        for event in events:
            if (event["kind"] == "custom_call" and event.get("turn_id") and
                    isinstance(event.get("input"), str) and "tools.apply_patch" in event["input"]):
                patch_paths_by_turn[event["turn_id"]].extend(patch_paths(event["input"]))
        for event in events:
            if (event["kind"] != "filechange" or event["mtime"] < lower or event["mtime"] > self.now
                    or event["change_status"] not in (None, "completed")):
                continue
            changes = event["changes"]
            if not isinstance(changes, dict):
                continue
            for raw, change in changes.items():
                if not isinstance(raw, str) or not isinstance(change, dict):
                    continue
                paths = [raw]
                move_path = change.get("move_path")
                if isinstance(move_path, str):
                    paths.append(move_path)
                paths.extend(patch_paths_by_turn.get(event.get("turn_id"), ()))
                for raw_path in paths:
                    path = resolve_event_path(raw_path, run["cwd"])
                    real = canonical_nonexistent(path)
                    if not any(path_allowed(real, root) for root in allowed):
                        allowed_text = json.dumps(sorted(set(allowed)), ensure_ascii=False)
                        self.add_signal(f"SIGNAL 3в {thread_id}: рамка; путь={escape(real)}; "
                                        f"разрешено={allowed_text}; источник={escape(str(event['path']))}:{event['line']}")

    def progress_checks(self, thread_id, events, intervals, run):
        window = float(self.config["thresholds"]["stagnation_minutes"]) * 60
        active_start = continuous_start(intervals, self.now)
        if active_start is None or self.now - active_start < window:
            return
        matching_files = [journal for journal in self.journals.get(thread_id, [])
                          if journal["account"] == run["account"] and journal["bad_line"] is None]
        in_horizon = [journal for journal in matching_files
                      if self.now - window <= journal["mtime"] <= self.now]
        out_horizon = [journal for journal in matching_files if journal["mtime"] < self.now - window]
        if in_horizon and out_horizon:
            source = sorted(in_horizon, key=lambda row: str(row["path"]))[0]["path"]
            self.source_missing(run, "история", source, thread_id=thread_id)
            return
        steps = {}
        for journal in in_horizon:
            for event in journal["events"]:
                if event["inherited_prefix"] or event["owner"] != thread_id or event["kind"] != "message":
                    continue
                for text in event.get("progress", []):
                    steps[text] = min(steps.get(text, event["epoch"]), event["epoch"])
        if any(timestamp >= self.now - window for timestamp in steps.values()):
            return
        source = (sorted(in_horizon, key=lambda row: str(row["path"]))[0]["path"] if in_horizon else
                  sorted(matching_files, key=lambda row: str(row["path"]))[0]["path"] if matching_files else
                  run["session_path"])
        self.add_signal(f"SIGNAL 3б {thread_id}: застой; непрерывно={(self.now - active_start) / 60:.2f} мин; "
                        f"новых строк хода за {window / 60:.2f} мин=0; источник={escape(str(source))}")

    def pending_command_at_end(self, events, run):
        last = events[-1]
        if last["kind"] == "command" and last.get("status") in ("started", "running"):
            return True
        if last["kind"] == "command" and last.get("exit") is None and last.get("process_id") is not None:
            try:
                if self.proc_alive(int(last["process_id"])):
                    return True
            except (TypeError, ValueError):
                pass
        if (last["kind"] == "custom_call" and last.get("tool_name") == "exec" and
                isinstance(last.get("input"), str) and re.search(r"tools\.exec_command\s*\(", last["input"])):
            call_id = last.get("call_id")
            if call_id is not None and not any(
                    event["kind"] == "custom_output" and event.get("call_id") == call_id and
                    event["epoch"] >= last["epoch"] for event in events):
                return True
        if last["kind"] == "custom_output":
            live = False
            try:
                live = self.proc_alive(int(last.get("process_id")))
            except (TypeError, ValueError):
                pass
            if last.get("session_id") and (live or self.descendants_alive(run["pid"])):
                return True
            if (re.search(r"(?:process|session)[ _-]?(?:id|session)[=: ]+[\w-]+",
                          last.get("output_text", ""), re.IGNORECASE) and
                    self.descendants_alive(run["pid"])):
                return True
        return False

    def idle_checks(self, thread_id, events, run):
        idle = float(self.config["thresholds"]["idle_minutes"])
        rows = [event for event in events if event["epoch"] <= self.now]
        if not rows:
            return
        last = rows[-1]
        age = (self.now - last["epoch"]) / 60
        if age <= idle:
            return
        if self.pending_command_at_end(rows, run) and self.descendants_alive(run["pid"]):
            return
        self.add_signal(f"SIGNAL 2а {thread_id}: простой; без событий={age:.2f} мин; порог={idle:.2f} мин; "
                        f"источник={escape(str(last['path']))}:{last['line']}")

    def helper_intervals(self, parent_id, parent_events, run):
        by_helper = collections.defaultdict(list)
        for event in parent_events:
            if event["kind"] != "subagent" or event["owner"] != parent_id:
                continue
            helper = event.get("agent_id")
            if isinstance(helper, str):
                by_helper[helper.lower()].append((event["epoch"], event.get("activity"), event))
        for helper, journals in self.journals.items():
            if any(journal["account"] == run["account"] and journal.get("parent") == parent_id
                   for journal in journals):
                by_helper.setdefault(helper, [])
        intervals = []
        unknown = set()
        live_helpers = set()
        for helper, records in by_helper.items():
            parent_activity_events = []
            for timestamp, kind, event in records:
                if kind == "started":
                    parent_activity_events.append((timestamp, 0, event))
                elif kind in ("completed", "interrupted"):
                    parent_activity_events.append((timestamp, 1, event))
            child_journals = [journal for journal in self.journals.get(helper, [])
                              if journal["account"] == run["account"]]
            usable = [journal for journal in child_journals if journal["bad_line"] is None]
            bad = [journal for journal in child_journals if journal["bad_line"] is not None]
            own_activity_events = []
            for journal in usable:
                own = [event for event in journal["events"]
                       if not event["inherited_prefix"] and event["owner"] == helper and
                       event["kind"] == "task_boundary"]
                for event in own:
                    if event["event_type"] == "task_started":
                        own_activity_events.append((event["epoch"], 0, event))
                    elif event["event_type"] in ("task_complete", "turn_aborted"):
                        own_activity_events.append((event["epoch"], 1, event))
            activity_events = own_activity_events or parent_activity_events
            activity_events.sort(key=lambda row: (row[0], str(row[2]["path"]), row[2]["line"]))
            opened = None
            evidence = None
            activity_intervals = []
            for timestamp, terminal, event in activity_events:
                if terminal:
                    if opened is not None:
                        activity_intervals.append((opened, timestamp, event))
                        opened = None
                        evidence = None
                else:
                    if opened is None:
                        opened = timestamp
                    evidence = event
            if opened is not None:
                live_helpers.add(helper)
                activity_intervals.append((opened, self.now, evidence))
            open_parent = helper in live_helpers
            if open_parent and bad:
                source = sorted(bad, key=lambda journal: str(journal["path"]))[0]
                self.add_signal(f"SIGNAL 5 {helper}: нет данных; прогон={escape(run['id'])}; "
                                f"журнал неразбираем, строка {source['bad_line']}; "
                                f"возраст={(self.now - run['started']) / 60:.2f} мин; "
                                f"источник={escape(str(source['path']))}")
                unknown.add(helper)
            elif open_parent and not usable:
                launch = opened
                source = self.rollout_glob(run["account"], helper)
                self.source_missing(run, "журнал", source, thread_id=helper, age_start=launch)
                self.helper_missing[helper] = (run, source, launch)
                unknown.add(helper)
            elif open_parent and usable and not any(journal["boundary_proven"] for journal in usable):
                source = sorted(usable, key=lambda journal: str(journal["path"]))[0]["path"]
                self.add_signal(f"SIGNAL 5 {helper}: нет данных; прогон={escape(run['id'])}; "
                                f"история помощника без границы; возраст={(self.now - run['started']) / 60:.2f} мин; "
                                f"источник={escape(str(source))}")
                self.helper_unknown.add(helper)
                unknown.add(helper)
            lower = self.now - float(self.config["thresholds"]["window_minutes"]) * 60
            for start, end, event in activity_intervals:
                evidence_mtime = event.get("mtime", 0) if event else 0
                recent = lower <= evidence_mtime <= self.now
                intervals.append((helper, start, end, helper in unknown, recent))
        return intervals, unknown, live_helpers

    def cycle_checks(self, thread_id, events, related_edits=()):
        lower = self.now - float(self.config["thresholds"]["window_minutes"]) * 60
        counter = new_counter()
        by_key = collections.defaultdict(list)
        edits = list(related_edits) + [
            event["epoch"] for event in events
            if event["kind"] == "filechange" and lower <= event["mtime"] <= self.now and
            event.get("change_status") in (None, "completed")]
        for event in events:
            if (event["kind"] != "command" or event["epoch"] < lower or event["epoch"] > self.now
                    or event["mtime"] < lower or event["mtime"] > self.now):
                continue
            if event.get("exit") is None or event.get("status") in ("started", "running"):
                continue
            key, _ = record_attempt(counter, event.get("command"), event.get("exit"),
                                    event.get("output"), strict=True)
            segment = sum(edit < event["epoch"] for edit in edits)
            by_key[(key, segment)].append(event)
        minimum = self.config["thresholds"]["repeat_count"]
        for (_key, _segment), attempts in by_key.items():
            if len(attempts) < minimum:
                continue
            event = attempts[-1]
            command = escape(strict_norm(event.get("command")))
            digest = hashlib.sha256(str(event.get("output") or "").encode("utf-8")).hexdigest()
            self.add_signal(f"SIGNAL 3а {thread_id}: цикл; попыток={len(attempts)}; exit={event.get('exit')}; "
                            f"sha256={digest}; действие={command}; "
                            f"источник={escape(str(event['path']))}:{event['line']}")

    def mechanics_checks(self, run, events, helper_intervals, unknown_helpers):
        threshold = self.config["thresholds"]["leader_command_count"]
        limit = run["account_data"]["helper_limit"]
        lower = self.now - float(self.config["thresholds"]["window_minutes"]) * 60
        commands = [event for event in events if event["kind"] == "command" and
                    event["epoch"] >= lower and event["epoch"] <= self.now and
                    event["mtime"] >= lower and event["mtime"] <= self.now and
                    event.get("exit") is not None and event.get("status") not in ("started", "running")]
        dedup = {}
        for event in commands:
            item_id = event.get("item_id")
            marker = ("id", item_id) if item_id is not None else (
                "event", event["epoch"], strict_norm(event.get("command")), event.get("exit"),
                hashlib.sha256(str(event.get("output") or "").encode("utf-8")).hexdigest())
            dedup[marker] = event
        commands = sorted(dedup.values(), key=lambda event: (event["epoch"], event["line"]))
        interval_unknown = any(start <= self.now and end > lower and not recent
                               for _helper, start, end, _unknown, recent in helper_intervals)
        if len(commands) <= threshold or unknown_helpers or interval_unknown:
            return
        points = []
        for helper, start, end, _unknown, _recent in helper_intervals:
            if end <= lower or start > self.now:
                continue
            points.append((max(start, lower), 1))
            points.append((min(end, self.now), -1))
        count = 0
        peak = 0
        for _, change in sorted(points, key=lambda point: (point[0], point[1])):
            count += change
            peak = max(peak, count)
        if peak >= limit:
            return
        event = commands[-1]
        self.add_signal(f"SIGNAL 4б {run['session']}: дроби; команд={len(commands)}; порог={threshold}; "
                        f"максимум помощников на интервале={peak}; предел={limit}; "
                        f"источник={escape(str(event['path']))}:{event['line']}")

    def activity_checks(self):
        for run in self.runs:
            if not run["active"] or not run["session"]:
                continue
            self.tick(run["session_path"])
            root_id = run["session"].lower()
            root_journals = [journal for journal in self.journals.get(root_id, [])
                             if journal["account"] == run["account"]]
            if any(journal["bad_line"] is not None for journal in root_journals):
                continue
            root_events = self.all_events(root_id, run["account"])
            if not root_events:
                continue
            root_intervals = active_intervals(root_id, self.runs, self.now)
            queue = collections.deque([root_id])
            seen = set()
            all_helpers = []
            unknown_helpers = set()
            live_helpers = set()
            thread_events = {root_id: root_events}
            while queue:
                parent = queue.popleft()
                if parent in seen:
                    continue
                seen.add(parent)
                parent_events = thread_events.get(parent) or self.all_events(parent, run["account"])
                if not parent_events:
                    continue
                intervals, unknown, live = self.helper_intervals(parent, parent_events, run)
                all_helpers.extend(intervals)
                unknown_helpers.update(unknown)
                live_helpers.update(live)
                for child in {entry[0] for entry in intervals}:
                    child_events = self.all_events(child, run["account"])
                    thread_events[child] = child_events
                    if child_events:
                        queue.append(child)
            descendant_edits = []
            lower = self.now - float(self.config["thresholds"]["window_minutes"]) * 60
            for child, events in thread_events.items():
                if child == root_id:
                    continue
                descendant_edits.extend(event["epoch"] for event in events
                                        if event["kind"] == "filechange" and lower <= event["mtime"] <= self.now and
                                        event.get("change_status") in (None, "completed"))
            self.idle_checks(root_id, root_events, run)
            self.cycle_checks(root_id, root_events, descendant_edits)
            self.frame_checks(root_id, root_events, run)
            self.progress_checks(root_id, root_events, root_intervals, run)
            for helper in sorted(thread_events):
                if helper == root_id:
                    continue
                events = thread_events[helper]
                if not events or helper in unknown_helpers:
                    continue
                intervals = [(start, end) for owner, start, end, _, _ in all_helpers if owner == helper]
                is_live = helper in live_helpers
                if is_live:
                    self.idle_checks(helper, events, run)
                    self.cycle_checks(helper, events)
                    self.frame_checks(helper, events, run)
                    self.progress_checks(helper, events, intervals, run)
            self.mechanics_checks(run, root_events, all_helpers, unknown_helpers)

    def all_events(self, thread_id, account=None):
        events = []
        for journals in self.journals.values():
            for journal in journals:
                self.tick(journal["path"])
                if journal["bad_line"] is not None or (account is not None and journal["account"] != account):
                    continue
                events.extend(event for event in journal["events"]
                              if not event["inherited_prefix"] and event["owner"] == thread_id.lower())
        events.sort(key=lambda event: (event["epoch"], str(event["path"]), event["line"]))
        seen = {}
        other = []
        for event in events:
            item_id = event.get("item_id")
            if event["kind"] == "command" and item_id is not None:
                key = (event["owner"], item_id)
                previous = seen.get(key)
                terminal = event.get("status") not in ("started", "running") or event.get("exit") is not None
                previous_terminal = previous and (
                    previous.get("status") not in ("started", "running") or previous.get("exit") is not None)
                if previous is None or (terminal and not previous_terminal) or (
                        terminal == previous_terminal and (event["epoch"], event["line"]) >=
                        (previous["epoch"], previous["line"])):
                    seen[key] = event
            else:
                other.append(event)
        result = other + list(seen.values())
        result.sort(key=lambda event: (event["epoch"], str(event["path"]), event["line"]))
        return result

    def waiting_and_load(self, plan):
        self.plan_waiting_checks(plan)
        ready = self.ready_counts(plan) if plan is not None else collections.defaultdict(set)
        self.capacity_checks(ready, plan)
        self.rate_checks()
        self.activity_checks()

    def preflight_outputs(self):
        for key in ("report_path", "signal_path"):
            path = Path(self.config[key])
            if path.exists() and not path.is_file():
                raise InputError(f"{path}: output path is not a regular file")
            if not path.parent.is_dir():
                raise InputError(f"{path}: output directory does not exist")
        report = Path(self.config["report_path"])
        signal_path = Path(self.config["signal_path"])
        if os.path.realpath(report) == os.path.realpath(signal_path):
            raise InputError(f"{self.config_path}: report_path and signal_path must differ")

    def signal_append(self):
        path = Path(self.config["signal_path"])
        current = self.read_text(path) if path.exists() else ""
        signal_lines = sorted(self.signals)
        if not signal_lines:
            return
        previous = previous_load(current)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if previous and previous["signals"] == signal_lines:
            entry = f"LOAD-{stamp}: повтор LOAD-{previous['first']}\n"
        else:
            entry = f"## LOAD-{stamp}\n" + "".join(line + "\n" for line in signal_lines)
        separator = "\n" if current and not current.endswith("\n") else ""
        try:
            with self.timed(path):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(separator + entry)
        except (OSError, UnicodeError) as exc:
            raise InputError(f"{path}: {exc}") from None

    def run(self):
        with self.overall_timer():
            return self._run()

    def _run(self):
        self.load_config()
        self.scan_runs()
        self.sessions_inventory()
        self.load_journals()
        plan = self.read_plan()
        self.waiting_and_load(plan)
        self.preflight_outputs()
        lines = sorted(self.signals | self.pending)
        if not lines:
            lines = ["OK"]
        report = Path(self.config["report_path"])
        try:
            with self.timed(report):
                report.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InputError(f"{report}: {exc}") from None
        self.signal_append()
        return lines, 3 if self.signals else 0


def parse_epoch(value, path, config_path):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise InputError(f"{path}: invalid timestamp (config {config_path})") from None


def patch_paths(value):
    match = re.search(r"tools\.apply_patch\s*\((.*)\)", value, re.DOTALL)
    if not match:
        return []
    argument = match.group(1).strip()
    try:
        patch = json.loads(argument)
    except ValueError:
        patch = argument.replace("\\n", "\n").strip().strip("\"'")
    if not isinstance(patch, str):
        return []
    paths = []
    current = None
    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", line)
        if match:
            current = match.group(1).strip().strip("\"'")
            paths.append(current)
            continue
        match = re.match(r"^\*\*\* Move to:\s*(.+?)\s*$", line)
        if match:
            if current:
                paths.append(current)
            paths.append(match.group(1).strip().strip("\"'"))
    return list(dict.fromkeys(paths))


def split_table_row(line):
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip().replace("\\|", "|") for cell in value.split("|")]


def progress_lines(text):
    result = []
    fenced = False
    fence_char = None
    backticks = chr(96) * 3
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(backticks) or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not fenced:
                fenced = True
                fence_char = marker
            elif marker == fence_char:
                fenced = False
            continue
        if fenced or stripped.startswith(">"):
            continue
        match = re.match(r"^\s*ХОД:\s*\[x\]\s*(.*?)\s*$", line)
        if match:
            normalized = " ".join(match.group(1).split())
            if normalized:
                result.append(normalized)
    return result


def active_intervals(thread_id, runs, now):
    intervals = []
    for run in runs:
        if run["session"] and run["session"].lower() == thread_id.lower():
            if run["active"]:
                intervals.append((run["started"], now))
            elif run["finished"] is not None:
                intervals.append((run["started"], run["finished"]))
    return intervals


def continuous_start(intervals, now):
    intervals = sorted((start, end) for start, end in intervals if start <= now)
    active = [interval for interval in intervals if interval[1] >= now]
    if not active:
        return None
    start, end = active[-1]
    for previous_start, previous_end in reversed(intervals):
        if previous_start >= start:
            continue
        if previous_end < start:
            break
        start = min(start, previous_start)
    return start


def escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def resolve_event_path(value, cwd):
    parsed = urlparse(value)
    if parsed.scheme == "file":
        value = unquote(parsed.path)
    if not os.path.isabs(value):
        value = os.path.join(cwd, value)
    return os.path.abspath(value)


def canonical_nonexistent(path):
    current = os.path.abspath(path)
    suffix = []
    while not os.path.lexists(current):
        parent, name = os.path.split(current)
        if parent == current:
            break
        suffix.append(name)
        current = parent
    root = os.path.realpath(current)
    return os.path.join(root, *reversed(suffix)) if suffix else root


def is_within(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def path_allowed(path, root):
    return path == root if os.path.isfile(root) else is_within(path, root)


def previous_load(text):
    lines = text.splitlines()
    if not lines:
        return None
    repeat = re.fullmatch(
        r"LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ): повтор LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)",
        lines[-1])
    if repeat:
        first = repeat.group(2)
        for index in range(len(lines) - 1, -1, -1):
            if lines[index] == f"## LOAD-{first}":
                signals = []
                for line in lines[index + 1:]:
                    if line.startswith("## LOAD-") or line.startswith("LOAD-"):
                        break
                    if line.startswith("SIGNAL "):
                        signals.append(line)
                return {"first": first, "signals": sorted(signals)}
        return None
    start = None
    stamp = None
    for index in range(len(lines) - 1, -1, -1):
        match = re.fullmatch(r"## LOAD-(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", lines[index])
        if match:
            start, stamp = index, match.group(1)
            break
        if lines[index].startswith("LOAD-"):
            return None
    if start is None:
        return None
    signals = []
    for line in lines[start + 1:]:
        if line.startswith("## LOAD-") or line.startswith("LOAD-"):
            break
        if line.startswith("SIGNAL "):
            signals.append(line)
    return {"first": stamp, "signals": sorted(signals)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка загрузки Codex")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    checker = Checker(args.config)
    try:
        lines, code = checker.run()
    except InputTimeout:
        print(f"{checker.current_path}: превышен предел чтения/разбора {checker.limit:g} секунд", file=sys.stderr)
        return 2
    except InputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print(f"{checker.current_path}: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write("".join(line + "\n" for line in lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
