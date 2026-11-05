import json
import posixpath
import re
import shlex
import sys

DELEGATING_TOOLS = ('Agent', 'Task')
SHELL_OPERATORS = '();<>|&\n'
JS_ESCAPE = re.compile(r'\\(u\{[0-9a-fA-F]{1,5}\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r?\n|.)', re.S)
JS_SIMPLE_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0'}
EXEC_SPLIT = re.compile(r'(?=\bexec_command\b)')
TRUNCATED_OUTPUT = re.compile(r'^Warning: truncated output \(original token count: \d+\)', re.M)
ABSOLUTE_PATH = re.compile(r'^(?:/|[a-z]:/)')
SED_RANGE = re.compile(r'^(\d+|\$)(?:,(\d+|\$))?p$')
LAST_LINE = sys.maxsize


def literal_pattern(key):
    return re.compile(
        r'''\b''' + key + r'''["']?\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)''',
        re.S)


CMD_LITERAL = literal_pattern('cmd')
WORKDIR_LITERAL = literal_pattern('workdir')


def normalize(value):
    return value.replace('\\', '/').lower()


def canonical(value):
    return posixpath.normpath(normalize(value))


def resolve(path, directory):
    target = normalize(path)
    if directory and not ABSOLUTE_PATH.match(target):
        target = directory + '/' + target
    return posixpath.normpath(target)


def candidates(path, directory):
    return {canonical(path), resolve(path, directory)}


def mention_names(target, directories):
    yield target
    for directory in directories:
        if directory and target.startswith(directory + '/'):
            relative = target[len(directory) + 1:]
            yield relative
            yield './' + relative


def mentions_path(command, target):
    if not target:
        return False
    start = 0
    while True:
        index = command.find(target, start)
        if index < 0:
            return False
        end = index + len(target)
        before = command[index - 1] if index else ''
        after = command[end] if end < len(command) else ''
        if not is_path_character(before) and not is_path_character(after):
            return True
        start = index + 1


def is_path_character(value):
    return bool(value) and (value.isalnum() or value in '._/\\-:')


def js_unescape(text):
    def replace(match):
        code = match.group(1)
        if len(code) > 1 and code[0] in 'ux':
            return chr(int(code[1:].strip('{}'), 16))
        if code[0] in '\r\n':
            return ''
        return JS_SIMPLE_ESCAPES.get(code, code)

    return JS_ESCAPE.sub(replace, text)


def literal_value(match):
    return js_unescape(next(group for group in match.groups() if group is not None))


def claude_tool_uses(record):
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'tool_use' and isinstance(block.get('input'), dict):
            yield block.get('name'), block['input']


def codex_exec_source(record):
    payload = record.get('payload')
    if isinstance(payload, dict) and payload.get('type') == 'custom_tool_call' and payload.get('name') == 'exec':
        source = payload.get('input')
        if isinstance(source, str):
            return source
    return None


def codex_calls(source):
    # Codex записывает команду как строковый литерал в JavaScript-коде вызова tools.exec_command({cmd: "..."}).
    for part in EXEC_SPLIT.split(source):
        workdir = WORKDIR_LITERAL.search(part)
        yield (
            literal_value(workdir) if workdir else None,
            [literal_value(match) for match in CMD_LITERAL.finditer(part)])


def codex_output_text(record):
    payload = record.get('payload')
    if not isinstance(payload, dict) or payload.get('type') != 'custom_tool_call_output':
        return None
    output = payload.get('output')
    if isinstance(output, list):
        output = '\n'.join(
            block['text'] for block in output if isinstance(block, dict) and isinstance(block.get('text'), str))
    return output if isinstance(output, str) else ''


def record_cwd(record):
    payload = record.get('payload')
    if record.get('type') in ('session_meta', 'turn_context') and isinstance(payload, dict):
        value = payload.get('cwd')
    else:
        value = record.get('cwd')
    return value if isinstance(value, str) and value else None


def is_compaction_boundary(record):
    kind = record.get('type')
    return kind == 'compacted' or (kind == 'system' and record.get('subtype') == 'compact_boundary')


def is_shell_operator(token):
    return bool(token) and all(char in SHELL_OPERATORS for char in token)


def shell_tokens(command, posix):
    lexer = shlex.shlex(command, posix=posix, punctuation_chars=SHELL_OPERATORS)
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    return list(lexer)


def cd_directory(words, directory):
    if len(words) != 2 or words[1] == '-' or words[1].startswith('~'):
        return None
    return resolve(words[1], directory)


def chain_parts(command, directory):
    # Разбор в режиме posix съедает обратные слэши в пути Windows, а режим без posix не понимает экранирование.
    for posix in (True, False):
        try:
            tokens = shell_tokens(command, posix)
        except ValueError:
            continue
        current_directory = directory
        current = []
        for token in tokens + [';']:
            if not is_shell_operator(token):
                if not posix and len(token) > 1 and token[0] == token[-1] and token[0] in '\'"':
                    token = token[1:-1]
                current.append(token)
                continue
            previous = current[-1] if current else ''
            if token in ('>&', '<&') or (token in ('>', '>>') and previous.isdigit() and previous != '1'):
                continue
            yield current, token, current_directory
            if current[:1] == ['cd']:
                current_directory = cd_directory(current, current_directory)
            current = []


def visible_parts(command, directory):
    for words, operator, part_directory in chain_parts(command, directory):
        hides_output = (operator[0] == '|' and operator[:2] != '||') or operator in ('>', '>>', '&>', '&>>')
        if not hides_output:
            yield words, part_directory


def cat_arguments(command, directory):
    for words, part_directory in visible_parts(command, directory):
        if words[:1] == ['cat']:
            for word in words[1:]:
                yield word, part_directory


def sed_ranges(script):
    ranges = []
    for piece in script.split(';'):
        piece = piece.strip()
        if not piece:
            continue
        match = SED_RANGE.match(piece)
        if not match:
            return []
        start = LAST_LINE if match.group(1) == '$' else int(match.group(1))
        end = start if match.group(2) is None else LAST_LINE if match.group(2) == '$' else int(match.group(2))
        if start < 1:
            return []
        ranges.append((start, max(start, end)))
    return ranges


def sed_slices(command, directory):
    for words, part_directory in visible_parts(command, directory):
        if len(words) == 4 and words[:2] == ['sed', '-n']:
            ranges = sed_ranges(words[2])
            if ranges:
                yield words[3], part_directory, ranges


def command_directories(command, directory):
    return {part_directory for _, _, part_directory in chain_parts(command, directory)} | {directory}


def note_cat(command, directory, wanted):
    found = set()
    for argument, part_directory in cat_arguments(command, directory):
        found |= candidates(argument, part_directory) & wanted
    return found


def note_sed(command, directory, wanted):
    found = {}
    for argument, part_directory, ranges in sed_slices(command, directory):
        for target in candidates(argument, part_directory) & wanted:
            found.setdefault(target, []).extend(ranges)
    return found


def line_count(path):
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError:
        return 0
    return data.count(b'\n') + (1 if data and not data.endswith(b'\n') else 0)


def covers_file(ranges, total):
    reached = 0
    for start, end in sorted(ranges):
        if start > reached + 1:
            break
        reached = max(reached, min(end, total))
    return total > 0 and reached >= total


def sed_covered(slices, truncated, requested):
    covered = set()
    for target, by_key in slices.items():
        total = line_count(requested[target])
        ranges = [(min(start, total), min(end, total)) for key, found in by_key.items() if key not in truncated
                  for start, end in found]
        if covers_file(ranges, total):
            covered.add(target)
    return covered


def note_mentions(text, directories, wanted, found):
    normalized = normalize(text)
    for target in wanted:
        if any(mentions_path(normalized, name) for name in mention_names(target, directories)):
            found.add(target)


def collect_reads(journal, requested):
    complete = {}
    slices = {}
    partial = set()
    delegated = set()
    truncated = set()
    call_reads = {}
    invalid_lines = 0
    wanted = set(requested)
    cwd = None
    serial = 0

    with open(journal, encoding='utf-8') as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(record, dict):
                continue
            if is_compaction_boundary(record):
                complete.clear()
                slices.clear()
                partial.clear()
                delegated.clear()
                truncated.clear()
                call_reads.clear()
                continue
            cwd = record_cwd(record) or cwd
            base = canonical(cwd) if cwd else None
            serial += 1
            kind = record.get('type')
            if kind == 'assistant':
                for name, inputs in claude_tool_uses(record):
                    if name == 'Read':
                        path = inputs.get('file_path')
                        if not isinstance(path, str):
                            continue
                        found = candidates(path, base) & wanted
                        if 'offset' in inputs or 'limit' in inputs:
                            partial.update(found)
                            offset, limit = inputs.get('offset'), inputs.get('limit')
                            start = offset if type(offset) is int and offset > 0 else 1
                            # 2000 — число строк, которое Read отдаёт без limit.
                            count = limit if type(limit) is int and limit > 0 else 2000
                            for target in found:
                                slices.setdefault(target, {}).setdefault(serial, []).append(
                                    (start, start + count - 1))
                        else:
                            for target in found:
                                complete.setdefault(target, set()).add(serial)
                    elif name == 'Bash':
                        command = inputs.get('command')
                        if not isinstance(command, str):
                            continue
                        for target in note_cat(command, base, wanted):
                            complete.setdefault(target, set()).add(serial)
                        for target, ranges in note_sed(command, base, wanted).items():
                            slices.setdefault(target, {}).setdefault(serial, []).extend(ranges)
                        note_mentions(command, command_directories(command, base), wanted, partial)
                    elif name in DELEGATING_TOOLS:
                        prompt = inputs.get('prompt')
                        if isinstance(prompt, str):
                            note_mentions(prompt, [], wanted, delegated)
            elif kind == 'response_item':
                output = codex_output_text(record)
                if output is not None:
                    call_id = record['payload'].get('call_id')
                    if isinstance(call_id, str) and TRUNCATED_OUTPUT.search(output):
                        truncated.add(call_id)
                        partial.update(call_reads.get(call_id, ()))
                    continue
                source = codex_exec_source(record)
                if source is None:
                    continue
                call_id = record['payload'].get('call_id')
                key = call_id if isinstance(call_id, str) else serial
                directories = set()
                for workdir, commands in codex_calls(source):
                    call_directory = resolve(workdir, base) if workdir else base
                    if workdir:
                        directories.add(call_directory)
                    for command in commands:
                        directories |= command_directories(command, call_directory)
                        found = note_cat(command, call_directory, wanted)
                        call_reads.setdefault(key, set()).update(found)
                        for target in found:
                            complete.setdefault(target, set()).add(key)
                        for target, ranges in note_sed(command, call_directory, wanted).items():
                            slices.setdefault(target, {}).setdefault(key, []).extend(ranges)
                note_mentions(js_unescape(source), directories or {base}, wanted, partial)

    read = {target for target, keys in complete.items() if keys - truncated}
    return read | sed_covered(slices, truncated, requested), partial, delegated, invalid_lines


def main(argv):
    deleg_ok = '--deleg-ok' in argv[1:]
    args = [arg for arg in argv[1:] if arg != '--deleg-ok']
    if len(args) < 2:
        print('Usage: check-reads.py [--deleg-ok] <journal.jsonl> <path>...', file=sys.stderr)
        return 2

    journal = args[0]
    paths = args[1:]
    try:
        complete, partial, delegated, invalid_lines = collect_reads(
            journal, {canonical(path): path for path in paths})
    except OSError as error:
        print(f'Cannot read journal: {error}', file=sys.stderr)
        return 2

    ok = True
    for path in paths:
        target = canonical(path)
        if target in complete:
            status = 'READ'
        elif target in delegated:
            status = 'DELEG'
            ok = ok and deleg_ok
        elif target in partial:
            status = 'PART'
            ok = False
        else:
            status = 'MISS'
            ok = False
        print(f'{status} {path}')

    if invalid_lines:
        print(f'Invalid JSON lines: {invalid_lines}', file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
