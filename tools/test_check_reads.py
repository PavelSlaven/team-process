import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(TOOLS, 'check-reads.py')


def assistant(*blocks):
    return {'type': 'assistant', 'message': {'content': list(blocks)}}


def use(name, inputs):
    return {'type': 'tool_use', 'name': name, 'input': inputs}


class CheckReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmp.name, 'journal.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def write_journal(self, records):
        with open(self.journal, 'w', encoding='utf-8') as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + '\n')

    def run_cli(self, *paths):
        return subprocess.run([sys.executable, '-B', '-I', SCRIPT, self.journal, *paths],
                              capture_output=True, encoding='utf-8', stdin=subprocess.DEVNULL)

    def test_four_case_fixture_reports_status_in_argument_order(self):
        full = os.path.join(self.tmp.name, 'full.md')
        bash = os.path.join(self.tmp.name, 'bash.md')
        partial = os.path.join(self.tmp.name, 'partial.md')
        missing = os.path.join(self.tmp.name, 'missing.md')
        self.write_journal([
            assistant(use('Read', {'file_path': full})),
            assistant(use('Bash', {'command': f'cat {bash}'})),
            assistant(use('Read', {'file_path': partial, 'offset': 1})),
            {'type': 'user', 'message': {'content': [
                {'type': 'text', 'text': f'Read {missing}'},
                {'type': 'tool_result', 'content': f'cat {missing}'},
            ]}},
        ])

        result = self.run_cli(full, bash, partial, missing)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f'READ {full}', f'READ {bash}', f'PART {partial}', f'MISS {missing}'
        ])

    def test_all_read_returns_zero(self):
        first = os.path.join(self.tmp.name, 'first.md')
        second = os.path.join(self.tmp.name, 'second.md')
        self.write_journal([
            assistant(use('Read', {'file_path': first})),
            assistant(use('Bash', {'command': f'cat {second}'})),
        ])

        result = self.run_cli(first, second)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {first}', f'READ {second}'])

    def test_missing_journal_and_missing_paths_return_two(self):
        missing = os.path.join(self.tmp.name, 'absent.jsonl')
        result = subprocess.run([sys.executable, '-B', '-I', SCRIPT, missing, 'file.md'],
                                capture_output=True, encoding='utf-8', stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 2)

        result = self.run_cli()
        self.assertEqual(result.returncode, 2)

    def test_invalid_json_is_skipped_and_counted_on_stderr(self):
        target = os.path.join(self.tmp.name, 'read.md')
        self.write_journal([assistant(use('Read', {'file_path': target}))])
        with open(self.journal, 'a', encoding='utf-8') as fh:
            fh.write('{invalid json}\n')

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])
        self.assertTrue(result.stderr.strip().endswith('1'), result.stderr)

    def test_path_matching_changes_only_slashes_and_case(self):
        target = 'c:/Work/Docs/Readme.MD'
        bash_target = r'D:\Data\BashFile.md'
        self.write_journal([
            assistant(use('Read', {'file_path': r'C:\work\docs\README.md'})),
            assistant(use('Bash', {'command': "cat 'd:\\data\\bashfile.MD'"})),
        ])

        result = self.run_cli(target, bash_target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}', f'READ {bash_target}'])

    def test_offset_zero_and_limit_null_are_partial(self):
        offset_path = os.path.join(self.tmp.name, 'offset.md')
        limit_path = os.path.join(self.tmp.name, 'limit.md')
        self.write_journal([
            assistant(use('Read', {'file_path': offset_path, 'offset': 0})),
            assistant(use('Read', {'file_path': limit_path, 'limit': None})),
        ])

        result = self.run_cli(offset_path, limit_path)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'PART {offset_path}', f'PART {limit_path}'])

    def test_complete_read_after_partial_takes_priority(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([
            assistant(use('Read', {'file_path': target, 'offset': 0})),
            assistant(use('Read', {'file_path': target})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_bash_read_classification_uses_first_word_and_path_argument(self):
        paths = [
            '/docs/rules.md',
            'rules.md',
            'sed-rules.md',
            'cat-rules.md',
            'head-rules.md',
            'tail-rules.md',
            'broken-rules.md',
            'type-rules.md',
            'get-content-rules.md',
        ]
        commands = [
            f'echo cat {paths[0]}',
            'cat other.md',
            f'sed -n 1,20p {paths[2]}',
            f'cat {paths[3]}',
            f'head -n 5 {paths[4]}',
            f'tail -n 5 {paths[5]}',
            f'cat "{paths[6]}',
            f'type {paths[7]}',
            f'Get-Content {paths[8]}',
        ]
        self.write_journal([assistant(use('Bash', {'command': command})) for command in commands])

        result = self.run_cli(*paths)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f'PART {paths[0]}', f'MISS {paths[1]}', f'PART {paths[2]}',
            f'READ {paths[3]}', f'PART {paths[4]}', f'PART {paths[5]}',
            f'PART {paths[6]}', f'PART {paths[7]}', f'PART {paths[8]}'
        ])

    def test_cat_rules_md_is_a_complete_read(self):
        target = 'rules.md'
        self.write_journal([assistant(use('Bash', {'command': 'cat rules.md'}))])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_cat_backslash_escaped_path_with_spaces_is_a_complete_read(self):
        target = '/tmp/x/rules with spaces.md'
        self.write_journal([assistant(use(
            'Bash', {'command': r'cat /tmp/x/rules\ with\ spaces.md'}))])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_cat_double_quoted_path_with_spaces_is_a_complete_read(self):
        target = '/tmp/x/rules with spaces.md'
        self.write_journal([assistant(use(
            'Bash', {'command': 'cat "/tmp/x/rules with spaces.md"'}))])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])


if __name__ == '__main__':
    unittest.main()
