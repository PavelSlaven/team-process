import argparse
import datetime
import decimal
import glob
import json
import os
import re
import sys


DAILY_HEADER = [
    'день', 'всего', 'ведущий_главная', 'ведущий_суб_opus', 'ведущий_суб_sonnet',
    'ведущий_суб_haiku', 'ведущий_суб_прочие', 'прочие', 'доля_ведущего',
    'opus_суб_ведущего',
]
SESSION_HEADER = ['файл', 'модели', 'вызовов', 'вызовов_Agent', 'модели_Agent', 'взвешенные_млн']
MILLION = decimal.Decimal(1000000)
AGENT_MODEL = 'не задана'
PATH_PATTERN = re.compile(
    r'(?P<quoted>"[^"\r\n]*?\.md"|\'[^\'\r\n]*?\.md\'|`[^`\r\n]*?\.md`)'
    r'|(?P<path>[A-Za-z]:[\\/][^\s<>"\'`]*?\.md|/[^\s<>"\'`]*?\.md|[^\s<>"\'`]+?\.md)',
    re.IGNORECASE,
)


def log_paths(folder):
    paths = glob.glob(os.path.join(folder, '*.jsonl'))
    paths.extend(glob.glob(os.path.join(folder, '*', 'subagents', '*.jsonl')))
    return sorted(set(path for path in paths if os.path.isfile(path)))


def session_paths(folder, session_id):
    main = os.path.join(folder, session_id + '.jsonl')
    subs = sorted(glob.glob(os.path.join(folder, session_id, 'subagents', '*.jsonl')))
    paths = ([main] if os.path.isfile(main) else [])
    paths.extend(path for path in subs if os.path.isfile(path))
    return paths


def ordered_daily_paths(folder, leader_id):
    all_paths = log_paths(folder)
    leader_main = os.path.abspath(os.path.join(folder, leader_id + '.jsonl'))
    leader_dir = os.path.abspath(os.path.join(folder, leader_id, 'subagents'))
    own_subs = sorted(
        (path for path in all_paths if os.path.dirname(os.path.abspath(path)) == leader_dir),
        key=lambda path: os.path.basename(path),
    )
    ordered = ([leader_main] if leader_main in {os.path.abspath(p) for p in all_paths} else [])
    ordered.extend(own_subs)
    ordered.extend(sorted(
        (path for path in all_paths if os.path.abspath(path) not in {os.path.abspath(p) for p in ordered}),
        key=lambda path: os.path.relpath(path, folder).replace(os.sep, '/'),
    ))
    own_sub_set = {os.path.abspath(path) for path in own_subs}
    return ordered, leader_main, own_sub_set


def json_records(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def timestamp_utc(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def token_value(usage, key):
    if not isinstance(usage, dict):
        return decimal.Decimal(0)
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return decimal.Decimal(0)
    try:
        number = decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        return decimal.Decimal(0)
    return number if number.is_finite() else decimal.Decimal(0)


def weighted_tokens(message):
    usage = message.get('usage')
    return (
        token_value(usage, 'input_tokens')
        + decimal.Decimal('1.25') * token_value(usage, 'cache_creation_input_tokens')
        + decimal.Decimal('0.1') * token_value(usage, 'cache_read_input_tokens')
        + decimal.Decimal(5) * token_value(usage, 'output_tokens')
    )


def model_text(message):
    value = message.get('model')
    return value if isinstance(value, str) else ''


def model_category(model):
    lowered = model.casefold()
    for name in ('opus', 'sonnet', 'haiku'):
        if name in lowered:
            return name
    return 'прочие'


def daily_row():
    return {
        'ведущий_главная': decimal.Decimal(0),
        'ведущий_суб_opus': decimal.Decimal(0),
        'ведущий_суб_sonnet': decimal.Decimal(0),
        'ведущий_суб_haiku': decimal.Decimal(0),
        'ведущий_суб_прочие': decimal.Decimal(0),
        'прочие': decimal.Decimal(0),
        'first_sub_replies': {},
        'opus_files': {},
    }


def daily_totals(folder, leader_id, start_date):
    ordered, leader_main, own_subs = ordered_daily_paths(folder, leader_id)
    seen = set()
    days = {}
    for path in ordered:
        absolute = os.path.abspath(path)
        is_main = absolute == leader_main
        is_own_sub = absolute in own_subs
        for record in json_records(path):
            if record.get('type') != 'assistant':
                continue
            message = record.get('message')
            if not isinstance(message, dict):
                continue
            stamp = timestamp_utc(record.get('timestamp'))
            if stamp is None:
                continue
            message_id = message.get('id')
            first_id = False
            if isinstance(message_id, (str, int)) and not isinstance(message_id, bool) and message_id:
                if message_id not in seen:
                    seen.add(message_id)
                    first_id = True
            if stamp.date() < start_date:
                continue
            model = model_text(message)
            day = stamp.date().isoformat()
            if is_own_sub:
                aggregate = days.setdefault(day, daily_row())
                relpath = os.path.relpath(path, folder).replace(os.sep, '/')
                previous = aggregate['first_sub_replies'].get(relpath)
                if previous is None or stamp < previous[0]:
                    aggregate['first_sub_replies'][relpath] = (stamp, first_user_text(path))
                if 'opus' in model.casefold():
                    aggregate['opus_files'][relpath] = True

            if not first_id:
                continue
            aggregate = days.setdefault(day, daily_row())
            amount = weighted_tokens(message)
            if is_main:
                aggregate['ведущий_главная'] += amount
            elif is_own_sub:
                aggregate['ведущий_суб_' + model_category(model)] += amount
            else:
                aggregate['прочие'] += amount
    for aggregate in days.values():
        aggregate['opus_files'] = {
            path: aggregate['first_sub_replies'][path]
            for path in aggregate['opus_files']
        }
        del aggregate['first_sub_replies']
    return days


def format_millions(tokens):
    return format(tokens / MILLION, '.1f')


def format_daily(days, alarm):
    lines = ['\t'.join(DAILY_HEADER)]
    day_names = sorted(days)
    for day_name in day_names:
        row = days[day_name]
        total = sum((row[key] for key in DAILY_HEADER[2:8]), decimal.Decimal(0))
        leader = sum((row[key] for key in DAILY_HEADER[2:7]), decimal.Decimal(0))
        percent = decimal.Decimal(0)
        if total:
            percent = (leader * 100 / total).quantize(decimal.Decimal('1'), rounding=decimal.ROUND_HALF_UP)
        values = [
            day_name,
            format_millions(total),
            format_millions(row['ведущий_главная']),
            format_millions(row['ведущий_суб_opus']),
            format_millions(row['ведущий_суб_sonnet']),
            format_millions(row['ведущий_суб_haiku']),
            format_millions(row['ведущий_суб_прочие']),
            format_millions(row['прочие']),
            str(percent) + '%',
            str(len(row['opus_files'])),
        ]
        lines.append('\t'.join(values))

    if day_names:
        last_day = day_names[-1]
        row = days[last_day]
        lines.extend(['', '# Opus-субагенты ведущего ' + last_day])
        opus_files = sorted(
            row['opus_files'].items(),
            key=lambda item: (item[1][0], os.path.basename(item[0])),
        )
        for _, (stamp, text) in opus_files:
            lines.append(stamp.strftime('%H:%MZ') + '\t' + opus_description(text))

    alarms = []
    if alarm is not None:
        for day_name in day_names:
            row = days[day_name]
            total = sum((row[key] for key in DAILY_HEADER[2:8]), decimal.Decimal(0))
            if total / MILLION > alarm:
                alarms.append('ТРЕВОГА ' + day_name + ' ' + format_millions(total))
    lines.extend(alarms)
    return '\n'.join(lines) + '\n', (3 if alarms else 0)


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(
            block.get('text', '')
            for block in content
            if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
        )
    return ''


def first_user_text(path):
    for record in json_records(path):
        if record.get('type') != 'user':
            continue
        message = record.get('message')
        if isinstance(message, dict):
            return text_content(message.get('content'))
        return ''
    return ''


def opus_description(text):
    for match in PATH_PATTERN.finditer(text):
        candidate = match.group('quoted')
        if candidate is not None:
            candidate = candidate[1:-1]
        else:
            candidate = match.group('path')
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if line.strip():
                        return line.strip()
        except OSError:
            continue
    return text[:80]


def session_rows(folder, session_id):
    result = []
    for path in session_paths(folder, session_id):
        seen = set()
        models = []
        agent_models = []
        agent_calls = 0
        amount = decimal.Decimal(0)
        for record in json_records(path):
            if record.get('type') != 'assistant':
                continue
            message = record.get('message')
            if not isinstance(message, dict):
                continue
            message_id = message.get('id')
            if not isinstance(message_id, (str, int)) or isinstance(message_id, bool) or not message_id:
                continue
            if message_id in seen:
                continue
            seen.add(message_id)
            model = model_text(message) or AGENT_MODEL
            if model not in models:
                models.append(model)
            amount += weighted_tokens(message)
            content = message.get('content')
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get('type') != 'tool_use' or block.get('name') != 'Agent':
                    continue
                agent_calls += 1
                inputs = block.get('input')
                agent_model = inputs.get('model') if isinstance(inputs, dict) else None
                agent_models.append(agent_model if isinstance(agent_model, str) and agent_model else AGENT_MODEL)
        relative_path = os.path.relpath(path, folder).replace(os.sep, '/')
        result.append([
            relative_path,
            ','.join(models),
            str(len(seen)),
            str(agent_calls),
            ','.join(agent_models) if agent_models else AGENT_MODEL,
            format_millions(amount),
        ])
    return result


def parse_alarm(value):
    try:
        number = decimal.Decimal(value)
    except decimal.InvalidOperation as error:
        raise argparse.ArgumentTypeError('ожидается число в миллионах') from error
    if not number.is_finite():
        raise argparse.ArgumentTypeError('ожидается конечное число в миллионах')
    return number


def parse_start_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError(value)
    parsed = datetime.date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(value)
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('folder')
    parser.add_argument('leader_id', nargs='?')
    parser.add_argument('start_date', nargs='?')
    parser.add_argument('--alarm')
    parser.add_argument('--session')
    args = parser.parse_args(argv)

    if not os.path.isdir(args.folder):
        return 2
    if args.session is not None:
        if args.leader_id is not None or args.start_date is not None or args.alarm is not None:
            parser.error('режим --session не принимает дату или --alarm')
        print('\t'.join(SESSION_HEADER))
        for row in session_rows(args.folder, args.session):
            print('\t'.join(row))
        return 0
    if args.leader_id is None or args.start_date is None:
        parser.error('для дневного режима укажите id-ведущего и дату')
    try:
        start_date = parse_start_date(args.start_date)
    except ValueError:
        return 2
    alarm = None
    if args.alarm is not None:
        try:
            alarm = parse_alarm(args.alarm)
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
    output, status = format_daily(daily_totals(args.folder, args.leader_id, start_date), alarm)
    sys.stdout.write(output)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
