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


CLAUDE_COMPACTION = {'type': 'system', 'subtype': 'compact_boundary'}
CODEX_COMPACTION = {'type': 'compacted', 'payload': {'message': ''}}


def codex_exec(command):
    source = 'const r = await tools.exec_command({cmd:' + json.dumps(command) + '});\ntext(r);'
    return {'type': 'response_item', 'payload': {
        'type': 'custom_tool_call', 'name': 'exec', 'input': source}}


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

    def run_cli(self, *paths, deleg_ok=False):
        flags = ['--deleg-ok'] if deleg_ok else []
        return subprocess.run([sys.executable, '-B', '-I', SCRIPT, *flags, self.journal, *paths],
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

    def test_cat_arguments_end_at_and_operator_so_later_path_is_part(self):
        self.write_journal([assistant(use('Bash', {'command': "cat a.md && sed -n '1,5p' b.md"}))])

        result = self.run_cli('a.md', 'b.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['READ a.md', 'PART b.md'])

    def test_cat_piped_to_another_command_is_part(self):
        self.write_journal([assistant(use('Bash', {'command': 'cat a.md | head'}))])

        result = self.run_cli('a.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['PART a.md'])

    def test_cat_redirected_to_file_with_greater_than_is_part(self):
        self.write_journal([assistant(use('Bash', {'command': 'cat a.md > out'}))])

        result = self.run_cli('a.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['PART a.md'])

    def test_cat_appended_to_file_with_double_greater_than_is_part(self):
        self.write_journal([assistant(use('Bash', {'command': 'cat a.md >> out'}))])

        result = self.run_cli('a.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['PART a.md'])

    def test_cat_after_semicolon_is_read(self):
        self.write_journal([assistant(use('Bash', {'command': 'cat a.md; cat b.md'}))])

        result = self.run_cli('a.md', 'b.md')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['READ a.md', 'READ b.md'])

    def test_cat_arguments_end_at_or_redirect_and_newline_operators(self):
        for command, expected in [
            ('cat a.md || cat b.md', ['READ a.md', 'READ b.md']),
            ('cat a.md 2>&1', ['READ a.md', 'MISS b.md']),
            ('cat a.md 2>/dev/null', ['READ a.md', 'MISS b.md']),
            ('cat a.md 2>&1 | head', ['PART a.md', 'MISS b.md']),
            ('cat < a.md', ['PART a.md', 'MISS b.md']),
            ('cat a.md\ncat b.md', ['READ a.md', 'READ b.md']),
            ('cat a.md\nsed -n 1p b.md', ['READ a.md', 'PART b.md']),
        ]:
            with self.subTest(command=command):
                self.write_journal([assistant(use('Bash', {'command': command}))])

                result = self.run_cli('a.md', 'b.md')

                self.assertEqual(result.stdout.splitlines(), expected)

    def test_codex_cat_arguments_end_at_operators(self):
        for command, expected in [
            ("cat a.md && sed -n '1,5p' b.md", ['READ a.md', 'PART b.md']),
            ('cat a.md | head', ['PART a.md', 'MISS b.md']),
            ('cat a.md; cat b.md', ['READ a.md', 'READ b.md']),
        ]:
            with self.subTest(command=command):
                self.write_journal([codex_exec(command)])

                result = self.run_cli('a.md', 'b.md')

                self.assertEqual(result.stdout.splitlines(), expected)

    def test_read_before_compaction_without_read_after_is_miss(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([
            assistant(use('Read', {'file_path': target})),
            CLAUDE_COMPACTION,
            assistant(use('Read', {'file_path': os.path.join(self.tmp.name, 'other.md')})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'MISS {target}'])

    def test_read_before_and_after_compaction_is_read(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([
            assistant(use('Read', {'file_path': target})),
            CLAUDE_COMPACTION,
            assistant(use('Read', {'file_path': target})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_only_the_last_compaction_boundary_counts(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([
            CLAUDE_COMPACTION,
            assistant(use('Read', {'file_path': target})),
            CLAUDE_COMPACTION,
        ])

        result = self.run_cli(target)

        self.assertEqual(result.stdout.splitlines(), [f'MISS {target}'])

    def test_agent_prompt_path_is_deleg_and_fails_without_flag(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([assistant(use('Agent', {'prompt': f'Read {target} fully'}))])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'DELEG {target}'])

    def test_agent_prompt_path_passes_with_deleg_ok(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([assistant(use('Agent', {'prompt': f'Read {target} fully'}))])

        result = self.run_cli(target, deleg_ok=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'DELEG {target}'])

    def test_deleg_ok_does_not_excuse_missing_or_partial_paths(self):
        delegated = os.path.join(self.tmp.name, 'delegated.md')
        partial = os.path.join(self.tmp.name, 'partial.md')
        missing = os.path.join(self.tmp.name, 'missing.md')
        self.write_journal([
            assistant(use('Task', {'prompt': f'Read {delegated}'})),
            assistant(use('Bash', {'command': f'sed -n 1,5p {partial}'})),
        ])

        result = self.run_cli(delegated, partial, missing, deleg_ok=True)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f'DELEG {delegated}', f'PART {partial}', f'MISS {missing}'])

    def test_agent_prompt_uses_path_boundaries(self):
        target = '/docs/rules.md'
        self.write_journal([assistant(use('Agent', {'prompt': 'Read /docs/rules.md.bak'}))])

        result = self.run_cli(target, deleg_ok=True)

        self.assertEqual(result.stdout.splitlines(), [f'MISS {target}'])

    def test_agent_with_path_and_read_of_same_path_is_read(self):
        target = os.path.join(self.tmp.name, 'target.md')
        self.write_journal([
            assistant(use('Agent', {'prompt': f'Read {target}'})),
            assistant(use('Read', {'file_path': target})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_codex_cat_is_read(self):
        target = '/srv/work/rules.md'
        self.write_journal([codex_exec(f'cat {target}')])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_codex_cat_quoted_path_with_spaces_is_read(self):
        target = '/srv/work/rules with spaces.md'
        self.write_journal([codex_exec(f'cat "{target}"')])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_codex_cat_chain_reads_every_cat_argument(self):
        first = '/srv/work/first.md'
        second = '/srv/work/second.md'
        self.write_journal([codex_exec(f'cat {first} && cat {second}')])

        result = self.run_cli(first, second)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {first}', f'READ {second}'])

    def test_codex_sed_is_part(self):
        target = '/srv/work/rules.md'
        self.write_journal([codex_exec(f"sed -n '1,40p' {target}")])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'PART {target}'])

    def test_codex_read_before_compaction_is_miss(self):
        target = '/srv/work/rules.md'
        self.write_journal([codex_exec(f'cat {target}'), CODEX_COMPACTION])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'MISS {target}'])

    def test_codex_read_before_and_after_compaction_is_read(self):
        target = '/srv/work/rules.md'
        self.write_journal([codex_exec(f'cat {target}'), CODEX_COMPACTION, codex_exec(f'cat {target}')])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_foreign_and_malformed_records_are_ignored(self):
        target = '/srv/work/rules.md'
        self.write_journal([
            [1, 2, 3],
            'text',
            {'type': 'response_item', 'payload': 'text'},
            {'type': 'response_item', 'payload': {
                'type': 'custom_tool_call', 'name': 'exec', 'input': None}},
            {'type': 'response_item', 'payload': {
                'type': 'function_call', 'name': 'send_message', 'arguments': f'cat {target}'}},
            {'type': 'assistant', 'message': 'text'},
            assistant(use('Agent', {'prompt': None}), 'text', use('Read', {'file_path': 7}), use('Bash', {})),
            codex_exec(f'cat {target}'),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])


if __name__ == '__main__':
    unittest.main()
