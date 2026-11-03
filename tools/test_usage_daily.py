import datetime
import json
import os
import subprocess
import tempfile
import unittest


TOOLS = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(TOOLS, 'usage-daily.py')


def assistant(mid, timestamp, model='claude-sonnet-4', usage=None, content=''):
    return {
        'type': 'assistant',
        'timestamp': timestamp,
        'message': {
            'id': mid,
            'model': model,
            'usage': usage or {},
            'content': content,
        },
    }


def user(content, timestamp='2026-11-01T06:00:00Z'):
    return {'type': 'user', 'timestamp': timestamp, 'message': {'content': content}}


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        for row in rows:
            if isinstance(row, str):
                fh.write(row + '\n')
            else:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')


class UsageDaily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'journals')
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            ['python3', '-B', '-I', SCRIPT, *args],
            capture_output=True,
            encoding='utf-8',
            stdin=subprocess.DEVNULL,
        )

    def make_daily_fixture(self):
        docs = os.path.join(self.tmp.name, 'briefs with spaces')
        os.makedirs(docs)
        first_doc = os.path.join(docs, 'first title.md')
        second_doc = os.path.join(docs, 'second.md')
        with open(first_doc, 'w', encoding='utf-8') as fh:
            fh.write('\n  \n# First brief\nbody\n')
        with open(second_doc, 'w', encoding='utf-8') as fh:
            fh.write('# Second brief\n')

        main = os.path.join(self.root, 'lead.jsonl')
        write_jsonl(main, [
            assistant('before-start', '2026-11-01T01:30:00+02:00', usage={'input_tokens': 90000000}),
            assistant('stale', '2026-10-31T22:00:00Z', usage={'input_tokens': 90000000}),
            assistant('shared', '2026-11-01T00:00:00Z', usage={'input_tokens': 1000000}),
            assistant('shared', '2026-11-01T00:01:00Z', usage={'input_tokens': 99000000}),
            assistant(
                'formula',
                '2026-11-02T00:30:00+02:00',
                usage={
                    'input_tokens': 100000,
                    'cache_creation_input_tokens': 40000,
                    'cache_read_input_tokens': 100000,
                    'output_tokens': 80000,
                },
            ),
            assistant(None, '2026-11-01T12:00:00Z', usage={'input_tokens': 70000000}),
            {'type': 'assistant', 'timestamp': '2026-11-01T13:00:00Z', 'message': []},
            '{invalid json',
        ])

        path_text = f'Read C:\\old\\missing.md, then "{first_doc}", then {second_doc}.'
        write_jsonl(os.path.join(self.root, 'lead', 'subagents', 'a-opus.jsonl'), [
            user([{'type': 'text', 'text': path_text}, {'type': 'image', 'source': {}}]),
            assistant('a-sonnet', '2026-11-01T08:00:00Z', 'claude-sonnet-4', {'input_tokens': 100000}),
            assistant('a-opus-1', '2026-11-01T08:02:00Z', 'claude-opus-4', {'input_tokens': 200000}),
            assistant('a-opus-2', '2026-11-01T08:03:00Z', 'claude-opus-4', {'input_tokens': 200000}),
        ])

        fallback = 'Fallback:' + ('x' * 90) + ' C:\\Users\\someone\\absent.md'
        write_jsonl(os.path.join(self.root, 'lead', 'subagents', 'b-opus.jsonl'), [
            user(fallback),
            assistant('shared', '2026-11-01T07:00:00Z', 'claude-opus-4', {'input_tokens': 50000000}),
        ])
        write_jsonl(os.path.join(self.root, 'lead', 'subagents', 'z-opus.jsonl'), [
            user('Z brief fallback'),
            assistant('z-opus', '2026-11-01T08:02:00Z', 'claude-opus-4', {'input_tokens': 100000}),
        ])
        write_jsonl(os.path.join(self.root, 'lead', 'subagents', 'c-sonnet.jsonl'), [
            assistant('c-sonnet', '2026-11-01T09:00:00Z', 'claude-sonnet-4', {'input_tokens': 200000}),
        ])
        write_jsonl(os.path.join(self.root, 'lead', 'subagents', 'd-haiku.jsonl'), [
            assistant('d-haiku', '2026-11-01T10:00:00Z', 'claude-haiku-4', {'input_tokens': 300000}),
        ])
        write_jsonl(os.path.join(self.root, 'lead', 'subagents', 'e-other.jsonl'), [
            assistant('e-other', '2026-11-01T11:00:00Z', 'model-x', {'input_tokens': 400000}),
        ])
        write_jsonl(os.path.join(self.root, 'other.jsonl'), [
            assistant('shared', '2026-11-01T12:00:00Z', 'claude-opus-4', {'input_tokens': 80000000}),
            assistant('stale', '2026-11-01T12:00:30Z', 'model-y', {'input_tokens': 90000000}),
            assistant('other-main', '2026-11-01T12:01:00Z', 'model-y', {'input_tokens': 600001}),
        ])
        write_jsonl(os.path.join(self.root, 'other', 'subagents', 'child.jsonl'), [
            assistant('other-child', '2026-11-01T12:02:00Z', 'model-z', {'input_tokens': 700000}),
        ])

    def test_daily_totals_dedup_utc_categories_opus_list_and_alarm(self):
        self.make_daily_fixture()
        result = self.run_cli(self.root, 'lead', '2026-11-01')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(
            lines[0].split('\t'),
            ['день', 'всего', 'ведущий_главная', 'ведущий_суб_opus', 'ведущий_суб_sonnet',
             'ведущий_суб_haiku', 'ведущий_суб_прочие', 'прочие', 'доля_ведущего',
             'opus_суб_ведущего'],
        )
        self.assertEqual(
            lines[1].split('\t'),
            ['2026-11-01', '4.4', '1.6', '0.5', '0.3', '0.3', '0.4', '1.3', '70%', '3'],
        )
        self.assertEqual(lines[2], '')
        self.assertEqual(lines[3], '# Opus-субагенты ведущего 2026-11-01')
        self.assertEqual(lines[4:], [
            '07:00Z\t' + ('Fallback:' + ('x' * 71)),
            '08:00Z\t# First brief',
            '08:02Z\tZ brief fallback',
        ])

        exact = self.run_cli(self.root, 'lead', '2026-11-01', '--alarm', '4.360001')
        self.assertEqual(exact.returncode, 0, exact.stderr)
        self.assertNotIn('ТРЕВОГА', exact.stdout)
        over = self.run_cli(self.root, 'lead', '2026-11-01', '--alarm', '4.36')
        self.assertEqual(over.returncode, 3, over.stderr)
        self.assertEqual(over.stdout.splitlines()[-1], 'ТРЕВОГА 2026-11-01 4.4')

        after_utc_boundary = self.run_cli(self.root, 'lead', '2026-11-02')
        self.assertEqual(after_utc_boundary.returncode, 0, after_utc_boundary.stderr)
        self.assertEqual(after_utc_boundary.stdout, 'день\tвсего\tведущий_главная\tведущий_суб_opus\tведущий_суб_sonnet\tведущий_суб_haiku\tведущий_суб_прочие\tпрочие\tдоля_ведущего\topus_суб_ведущего\n')

    def test_opus_description_accepts_unquoted_relative_path(self):
        with open(os.path.join(self.tmp.name, 'README.md'), 'w', encoding='utf-8') as fh:
            fh.write('\n  \n# Fixture README heading\nbody\n')

        for subagent, text, stamp in (
            ('a-opus', 'Прочитай README.md', '2026-11-01T00:01:00Z'),
            ('b-opus', 'Прочитай "README.md"', '2026-11-01T00:02:00Z'),
        ):
            path = os.path.join(self.root, 'lead', 'subagents', subagent + '.jsonl')
            write_jsonl(path, [
                user(text),
                assistant(subagent, stamp, 'claude-opus-4', {'input_tokens': 1}),
            ])

        result = subprocess.run(
            ['python3', '-B', '-I', SCRIPT, self.root, 'lead', '2026-11-01'],
            capture_output=True,
            encoding='utf-8',
            stdin=subprocess.DEVNULL,
            cwd=self.tmp.name,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[4:], [
            '00:01Z\t# Fixture README heading',
            '00:02Z\t# Fixture README heading',
        ])

    def test_current_partial_utc_day_is_included(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        day = now.date().isoformat()
        stamp = now.isoformat().replace('+00:00', 'Z')
        write_jsonl(os.path.join(self.root, 'lead.jsonl'), [
            assistant('today', stamp, usage={'input_tokens': 100000}),
        ])
        result = self.run_cli(self.root, 'lead', day)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1].split('\t')[0], day)
        self.assertEqual(result.stdout.splitlines()[1].split('\t')[1], '0.1')

    def test_empty_selection_and_invalid_inputs(self):
        write_jsonl(os.path.join(self.root, 'lead.jsonl'), [
            assistant('old', '2026-10-31T23:00:00Z', usage={'input_tokens': 1000000}),
        ])
        result = self.run_cli(self.root, 'lead', '2026-11-01')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)

        missing = self.run_cli(os.path.join(self.tmp.name, 'missing'), 'lead', '2026-11-01')
        self.assertEqual(missing.returncode, 2)
        bad_date = self.run_cli(self.root, 'lead', '2026-02-30')
        self.assertEqual(bad_date.returncode, 2)
        bad_format = self.run_cli(self.root, 'lead', '2026/11/01')
        self.assertEqual(bad_format.returncode, 2)

    def test_session_counts_unique_messages_and_agent_models(self):
        main = os.path.join(self.root, 'session-1.jsonl')
        blocks = [
            {'type': 'tool_use', 'name': 'Agent', 'input': {'model': 'gpt-luna'}},
            {'type': 'tool_use', 'name': 'Agent', 'input': {}},
            {'type': 'tool_use', 'name': 'Bash', 'input': {'model': 'ignored'}},
            {'type': 'tool_use', 'name': 'Agent', 'input': {'model': 'gpt-luna'}},
        ]
        write_jsonl(main, [
            assistant('m1', '2026-11-01T00:00:00Z', 'claude-sonnet-4', {'input_tokens': 100000}, blocks),
            assistant('m1', '2026-11-01T00:01:00Z', 'model-duplicate', {'input_tokens': 99000000}, [
                {'type': 'tool_use', 'name': 'Agent', 'input': {'model': 'ignored-duplicate'}},
            ]),
            assistant('m2', '2026-11-01T00:02:00Z', 'claude-opus-4', {'input_tokens': 500000}),
        ])
        write_jsonl(os.path.join(self.root, 'session-1', 'subagents', 'child.jsonl'), [
            assistant('s1', '2026-11-01T00:03:00Z', 'claude-haiku-4', {'output_tokens': 20000}),
            assistant('s2', '2026-11-01T00:04:00Z', 'claude-haiku-4', {'input_tokens': 80000}),
        ])

        result = self.run_cli(self.root, '--session', 'session-1')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [line.split('\t') for line in result.stdout.splitlines()]
        self.assertEqual(rows[0], ['файл', 'модели', 'вызовов', 'вызовов_Agent', 'модели_Agent', 'взвешенные_млн'])
        self.assertEqual(rows[1], ['session-1.jsonl', 'claude-sonnet-4,claude-opus-4', '2', '3',
                                  'gpt-luna,не задана,gpt-luna', '0.6'])
        self.assertEqual(rows[2], ['session-1/subagents/child.jsonl', 'claude-haiku-4', '2', '0',
                                  'не задана', '0.2'])


if __name__ == '__main__':
    unittest.main()
