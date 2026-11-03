import json
import os
import shlex
import sys


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


def collect_reads(journal, requested):
    complete = set()
    partial = set()
    invalid_lines = 0
    wanted = set(requested)

    with open(journal, encoding='utf-8') as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(record, dict) or record.get('type') != 'assistant':
                continue
            message = record.get('message')
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                    continue
                inputs = block.get('input')
                if not isinstance(inputs, dict):
                    continue
                name = block.get('name')
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
                    normalized_command = normalize(command)
                    try:
                        arguments = shlex.split(command)
                    except ValueError:
                        arguments = None
                    if arguments and arguments[0] == 'cat':
                        for argument in arguments[1:]:
                            target = normalize(argument)
                            if target in wanted:
                                complete.add(target)
                    for target in wanted:
                        if mentions_path(normalized_command, target):
                            partial.add(target)

    return complete, partial, invalid_lines


def main(argv):
    if len(argv) < 3:
        print('Usage: check-reads.py <journal.jsonl> <path>...', file=sys.stderr)
        return 2

    journal = argv[1]
    paths = argv[2:]
    try:
        complete, partial, invalid_lines = collect_reads(journal, [normalize(path) for path in paths])
    except OSError as error:
        print(f'Cannot read journal: {error}', file=sys.stderr)
        return 2

    for path in paths:
        target = normalize(path)
        if target in complete:
            status = 'READ'
        elif target in partial:
            status = 'PART'
        else:
            status = 'MISS'
        print(f'{status} {path}')

    if invalid_lines:
        print(f'Invalid JSON lines: {invalid_lines}', file=sys.stderr)
    return 0 if all(normalize(path) in complete for path in paths) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
