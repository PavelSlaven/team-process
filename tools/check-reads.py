import json
import re
import shlex
import sys

DELEGATING_TOOLS = ('Agent', 'Task')
SHELL_OPERATORS = '();<>|&\n'
CMD_LITERAL = re.compile(
    r'''\bcmd["']?\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)''',
    re.S)
JS_ESCAPE = re.compile(r'\\(u\{[0-9a-fA-F]{1,5}\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r?\n|.)', re.S)
JS_SIMPLE_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0'}


def normalize(value):
    return value.replace('\\', '/').lower()


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


def codex_commands(source):
    # Codex записывает команду как строковый литерал в JavaScript-коде вызова tools.exec_command({cmd: "..."}).
    for match in CMD_LITERAL.finditer(source):
        yield js_unescape(next(group for group in match.groups() if group is not None))


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


def cat_arguments(command):
    # Разбор в режиме posix съедает обратные слэши в пути Windows, а режим без posix не понимает экранирование.
    for posix in (True, False):
        try:
            tokens = shell_tokens(command, posix)
        except ValueError:
            continue
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
            hides_output = (token[0] == '|' and token[:2] != '||') or token in ('>', '>>', '&>', '&>>')
            if current[:1] == ['cat'] and not hides_output:
                yield from current[1:]
            current = []


def note_cat(command, wanted, complete):
    for argument in cat_arguments(command):
        target = normalize(argument)
        if target in wanted:
            complete.add(target)


def note_mentions(text, wanted, found):
    normalized = normalize(text)
    for target in wanted:
        if mentions_path(normalized, target):
            found.add(target)


def collect_reads(journal, requested):
    complete = set()
    partial = set()
    delegated = set()
    invalid_lines = 0
    wanted = set(requested)

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
                partial.clear()
                delegated.clear()
                continue
            kind = record.get('type')
            if kind == 'assistant':
                for name, inputs in claude_tool_uses(record):
                    if name == 'Read':
                        path = inputs.get('file_path')
                        if not isinstance(path, str):
                            continue
                        target = normalize(path)
                        if target not in wanted:
                            continue
                        if 'offset' in inputs or 'limit' in inputs:
                            partial.add(target)
                        else:
                            complete.add(target)
                    elif name == 'Bash':
                        command = inputs.get('command')
                        if not isinstance(command, str):
                            continue
                        note_cat(command, wanted, complete)
                        note_mentions(command, wanted, partial)
                    elif name in DELEGATING_TOOLS:
                        prompt = inputs.get('prompt')
                        if isinstance(prompt, str):
                            note_mentions(prompt, wanted, delegated)
            elif kind == 'response_item':
                source = codex_exec_source(record)
                if source is None:
                    continue
                for command in codex_commands(source):
                    note_cat(command, wanted, complete)
                note_mentions(js_unescape(source), wanted, partial)

    return complete, partial, delegated, invalid_lines


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
            journal, [normalize(path) for path in paths])
    except OSError as error:
        print(f'Cannot read journal: {error}', file=sys.stderr)
        return 2

    ok = True
    for path in paths:
        target = normalize(path)
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
