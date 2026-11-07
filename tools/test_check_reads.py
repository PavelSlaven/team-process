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


def codex_exec(command, call_id=None, workdir=None):
    options = 'cmd:' + json.dumps(command)
    if workdir is not None:
        options += ', workdir:' + json.dumps(workdir)
    source = 'const r = await tools.exec_command({' + options + '});\ntext(r);'
    payload = {'type': 'custom_tool_call', 'name': 'exec', 'input': source}
    if call_id is not None:
        payload['call_id'] = call_id
    return {'type': 'response_item', 'payload': payload}


def codex_output(call_id, *texts):
    return {'type': 'response_item', 'payload': {
        'type': 'custom_tool_call_output', 'call_id': call_id,
        'output': [{'type': 'input_text', 'text': text} for text in texts]}}


def codex_session_meta(cwd):
    return {'type': 'session_meta', 'payload': {'cwd': cwd}}


def codex_turn_context(cwd):
    return {'type': 'turn_context', 'payload': {'cwd': cwd}}


COMPLETE_HEADER = 'Script completed\nWall time 0.2 seconds\nOutput:\n'
TRUNCATION_WARNING = 'Warning: truncated output (original token count: 12528)\nTotal output lines: 1\n\n'


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

    def write_lines(self, name, count):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(''.join(f'line {n}\n' for n in range(1, count + 1)))
        return path

    def test_read_chunks_covering_every_line_are_read(self):
        target = self.write_lines('chunks.md', 10)
        self.write_journal([
            assistant(use('Read', {'file_path': target, 'limit': 4})),
            assistant(use('Read', {'file_path': target, 'offset': 4, 'limit': 7})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_read_chunks_with_gap_are_partial(self):
        target = self.write_lines('gap.md', 10)
        self.write_journal([
            assistant(use('Read', {'file_path': target, 'offset': 1, 'limit': 3})),
            assistant(use('Read', {'file_path': target, 'offset': 5, 'limit': 6})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'PART {target}'])

    def test_read_chunks_before_compaction_do_not_count(self):
        target = self.write_lines('compacted.md', 10)
        self.write_journal([
            assistant(use('Read', {'file_path': target, 'limit': 5})),
            CLAUDE_COMPACTION,
            assistant(use('Read', {'file_path': target, 'offset': 6, 'limit': 5})),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'PART {target}'])

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

    def test_codex_relative_cat_resolves_from_turn_context_cwd(self):
        root = '/srv/work/tests-961'
        self.write_journal([
            codex_turn_context(root),
            codex_exec('cat .t/in/goal.md ./.t/in/spec.md'),
        ])

        result = self.run_cli(f'{root}/.t/in/goal.md', f'{root}/.t/in/spec.md')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f'READ {root}/.t/in/goal.md', f'READ {root}/.t/in/spec.md'])

    def test_codex_last_cwd_wins_and_session_meta_is_the_fallback(self):
        self.write_journal([
            codex_session_meta('/srv/work/meta'),
            codex_exec('cat first.md'),
            codex_turn_context('/srv/work/old'),
            codex_turn_context('/srv/work/new'),
            codex_exec('cat rules.md'),
        ])

        result = self.run_cli('/srv/work/meta/first.md', '/srv/work/new/rules.md', '/srv/work/old/rules.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'READ /srv/work/meta/first.md', 'READ /srv/work/new/rules.md', 'MISS /srv/work/old/rules.md'])

    def test_codex_workdir_of_the_call_overrides_cwd(self):
        self.write_journal([
            codex_turn_context('/srv/work/a'),
            codex_exec('cat rules.md', workdir='/srv/work/b'),
        ])

        result = self.run_cli('/srv/work/b/rules.md', '/srv/work/a/rules.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['READ /srv/work/b/rules.md', 'MISS /srv/work/a/rules.md'])

    def test_codex_cd_in_the_chain_changes_directory_for_later_parts_only(self):
        self.write_journal([
            codex_turn_context('/srv/work/root'),
            codex_exec('cat before.md && cd /srv/work/other && cat after.md'),
            codex_exec('cd sub; cat inner.md'),
        ])

        result = self.run_cli(
            '/srv/work/root/before.md', '/srv/work/other/before.md',
            '/srv/work/other/after.md', '/srv/work/root/after.md',
            '/srv/work/root/sub/inner.md', '/srv/work/root/inner.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([line.split()[0] == 'READ' for line in result.stdout.splitlines()],
                         [True, False, True, False, True, False])

    def test_codex_cd_x_and_cat_y_reads_x_slash_y(self):
        self.write_journal([codex_turn_context('/srv/work/root'), codex_exec('cd /srv/work/x && cat y.md')])

        result = self.run_cli('/srv/work/x/y.md')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['READ /srv/work/x/y.md'])

    def test_codex_relative_path_without_match_is_miss_and_dotdot_is_resolved(self):
        self.write_journal([codex_turn_context('/srv/work/root'), codex_exec('cat other/z.md ../up.md')])

        result = self.run_cli('/srv/work/root/z.md', '/srv/work/root/up.md', '/srv/work/up.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'MISS /srv/work/root/z.md', 'MISS /srv/work/root/up.md', 'READ /srv/work/up.md'])

    def test_codex_relative_sed_is_part(self):
        self.write_journal([codex_turn_context('/srv/work/root'), codex_exec('sed -n 1,5p in/a.md')])

        result = self.run_cli('/srv/work/root/in/a.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['PART /srv/work/root/in/a.md'])

    def test_codex_cwd_survives_compaction(self):
        self.write_journal([
            codex_turn_context('/srv/work/root'),
            codex_exec('cat before.md'),
            CODEX_COMPACTION,
            codex_exec('cat after.md'),
        ])

        result = self.run_cli('/srv/work/root/before.md', '/srv/work/root/after.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'MISS /srv/work/root/before.md', 'READ /srv/work/root/after.md'])

    def test_codex_truncated_output_makes_every_file_of_the_call_part(self):
        root = '/srv/work/tests-961'
        self.write_journal([
            codex_turn_context(root),
            codex_exec('cat .t/goal.md .t/spec.md', call_id='call_cut'),
            codex_output('call_cut', COMPLETE_HEADER, TRUNCATION_WARNING + '{"output":"..."}'),
            codex_exec('cat .t/whole.md', call_id='call_whole'),
            codex_output('call_whole', COMPLETE_HEADER, 'text'),
        ])

        result = self.run_cli(f'{root}/.t/goal.md', f'{root}/.t/spec.md', f'{root}/.t/whole.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f'PART {root}/.t/goal.md', f'PART {root}/.t/spec.md', f'READ {root}/.t/whole.md'])

    def test_codex_truncated_output_of_absolute_cat_is_part(self):
        target = '/srv/work/abs.md'
        self.write_journal([
            codex_exec(f'cat {target}', call_id='call_cut'),
            codex_output('call_cut', TRUNCATION_WARNING + 'x'),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'PART {target}'])

    def test_codex_whole_cat_in_another_call_beats_truncated_one(self):
        target = '/srv/work/twice.md'
        self.write_journal([
            codex_exec(f'cat {target}', call_id='call_cut'),
            codex_output('call_cut', TRUNCATION_WARNING + 'x'),
            codex_exec(f'cat {target}', call_id='call_whole'),
            codex_output('call_whole', COMPLETE_HEADER, 'text'),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_codex_warning_text_inside_a_line_is_not_truncation(self):
        target = '/srv/work/note.md'
        self.write_journal([
            codex_exec(f'cat {target}', call_id='call_note'),
            codex_output('call_note', COMPLETE_HEADER, 'He wrote Warning: truncated output (original token count: 5)'),
        ])

        result = self.run_cli(target)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {target}'])

    def test_claude_record_cwd_resolves_relative_cat_and_cd(self):
        self.write_journal([
            {'type': 'assistant', 'cwd': '/srv/work/root', 'message': {'content': [
                use('Bash', {'command': 'cat in/a.md'}),
                use('Bash', {'command': 'cd sub && cat b.md'}),
                use('Read', {'file_path': 'c.md', 'limit': 5}),
            ]}},
        ])

        result = self.run_cli(
            '/srv/work/root/in/a.md', '/srv/work/root/sub/b.md', '/srv/work/root/b.md', '/srv/work/root/c.md')

        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:2], ['READ /srv/work/root/in/a.md', 'READ /srv/work/root/sub/b.md'])
        self.assertFalse(lines[2].startswith('READ'), lines[2])
        self.assertEqual(lines[3], 'PART /srv/work/root/c.md')

    def make_file(self, name, lines, final_newline=True):
        path = os.path.join(self.tmp.name, name)
        text = '\n'.join(f'line {number}' for number in range(1, lines + 1)) + ('\n' if final_newline else '')
        with open(path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)
        return path.replace('\\', '/')

    def sed_journal(self, path, *scripts, truncated=()):
        records = []
        for index, script in enumerate(scripts):
            call_id = f'call_{index}'
            records.append(codex_exec(f"sed -n '{script}' {path}", call_id=call_id))
            text = TRUNCATION_WARNING + 'x' if index in truncated else 'text'
            records.append(codex_output(call_id, COMPLETE_HEADER, text))
        return records

    def assert_status(self, path, status, returncode):
        result = self.run_cli(path)
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'{status} {path}'])

    def test_codex_sed_parts_covering_the_whole_file_are_read(self):
        path = self.make_file('scout.md', 155)
        self.write_journal(self.sed_journal(path, '1,55p', '56,112p', '113,155p'))

        self.assert_status(path, 'READ', 0)

    def test_codex_sed_parts_in_any_order_and_overlapping_are_read(self):
        path = self.make_file('scout.md', 30)
        self.write_journal(self.sed_journal(path, '20,30p', '1,12p', '10,22p'))

        self.assert_status(path, 'READ', 0)

    def test_codex_sed_hole_in_the_middle_is_part(self):
        path = self.make_file('scout.md', 155)
        self.write_journal(self.sed_journal(path, '1,55p', '57,112p', '113,155p'))

        self.assert_status(path, 'PART', 1)

    def test_codex_sed_missing_start_or_end_is_part(self):
        path = self.make_file('scout.md', 20)
        self.write_journal(self.sed_journal(path, '2,20p'))
        self.assert_status(path, 'PART', 1)
        self.write_journal(self.sed_journal(path, '1,19p'))
        self.assert_status(path, 'PART', 1)

    def test_codex_sed_truncated_part_adds_no_coverage(self):
        path = self.make_file('scout.md', 155)
        self.write_journal(self.sed_journal(path, '1,55p', '56,112p', '113,155p', truncated=(1,)))

        self.assert_status(path, 'PART', 1)

    def test_codex_sed_dollar_as_the_range_end_is_read(self):
        path = self.make_file('scout.md', 155)
        self.write_journal(self.sed_journal(path, '1,100p', '101,$p'))

        self.assert_status(path, 'READ', 0)

    def test_codex_sed_single_dollar_and_single_line_ranges_are_counted(self):
        path = self.make_file('scout.md', 3)
        self.write_journal(self.sed_journal(path, '1p', '2p', '$p'))

        self.assert_status(path, 'READ', 0)

    def test_codex_sed_range_end_beyond_the_file_is_clipped(self):
        path = self.make_file('scout.md', 20)
        self.write_journal(self.sed_journal(path, '1,500p'))

        self.assert_status(path, 'READ', 0)

    def test_codex_sed_several_ranges_in_one_script_are_counted(self):
        path = self.make_file('scout.md', 20)
        self.write_journal(self.sed_journal(path, '1,10p;11,20p'))
        self.assert_status(path, 'READ', 0)
        self.write_journal(self.sed_journal(path, '1,10p;12,20p'))
        self.assert_status(path, 'PART', 1)

    def test_codex_sed_unknown_script_adds_no_coverage(self):
        path = self.make_file('scout.md', 20)
        self.write_journal(self.sed_journal(path, '1,20d', '1,20p;s/a/b/'))

        self.assert_status(path, 'PART', 1)

    def test_codex_sed_commands_joined_in_one_call_with_relative_paths_are_read(self):
        root = self.tmp.name.replace('\\', '/')
        self.make_file('out.md', 155)
        command = "sed -n '1,55p' out.md; sed -n '56,112p' out.md; sed -n '113,155p' out.md"
        self.write_journal([
            codex_turn_context(root),
            codex_exec(command, call_id='call_all'),
            codex_output('call_all', COMPLETE_HEADER, 'text'),
        ])

        self.assert_status(f'{root}/out.md', 'READ', 0)

    def test_codex_sed_joined_call_with_truncated_output_adds_no_coverage(self):
        root = self.tmp.name.replace('\\', '/')
        self.make_file('out.md', 155)
        command = "sed -n '1,55p' out.md; sed -n '56,155p' out.md"
        self.write_journal([
            codex_turn_context(root),
            codex_exec(command, call_id='call_all'),
            codex_output('call_all', COMPLETE_HEADER, TRUNCATION_WARNING + 'x'),
        ])

        self.assert_status(f'{root}/out.md', 'PART', 1)

    def test_codex_sed_workdir_and_cd_resolve_relative_paths(self):
        root = self.tmp.name.replace('\\', '/')
        self.make_file('out.md', 20)
        self.write_journal([
            codex_exec("sed -n '1,10p' out.md", workdir=root),
            codex_exec(f"cd {root} && sed -n '11,20p' out.md"),
        ])

        self.assert_status(f'{root}/out.md', 'READ', 0)

    def test_codex_sed_file_without_final_newline_counts_the_last_line(self):
        path = self.make_file('scout.md', 20, final_newline=False)
        self.write_journal(self.sed_journal(path, '1,19p'))
        self.assert_status(path, 'PART', 1)
        self.write_journal(self.sed_journal(path, '1,20p'))
        self.assert_status(path, 'READ', 0)

    def test_codex_sed_missing_file_adds_no_coverage(self):
        path = self.tmp.name.replace('\\', '/') + '/absent.md'
        self.write_journal(self.sed_journal(path, '1,500p'))

        self.assert_status(path, 'PART', 1)

    def test_codex_sed_in_a_pipe_adds_no_coverage(self):
        path = self.make_file('scout.md', 20)
        self.write_journal([codex_exec(f"sed -n '1,20p' {path} | head -5", call_id='call_pipe')])

        self.assert_status(path, 'PART', 1)

    def test_codex_sed_coverage_is_reset_by_compaction(self):
        path = self.make_file('scout.md', 155)
        before = self.sed_journal(path, '1,55p', '56,112p')
        after = self.sed_journal(path, '113,155p')
        self.write_journal(before + [CODEX_COMPACTION] + after)
        self.assert_status(path, 'PART', 1)

        self.write_journal(before + [CODEX_COMPACTION])
        self.assert_status(path, 'MISS', 1)

    def test_claude_sed_parts_covering_the_whole_file_are_read(self):
        path = self.make_file('scout.md', 20)
        self.write_journal([
            assistant(use('Bash', {'command': f"sed -n '1,10p' {path}"})),
            assistant(use('Bash', {'command': f"sed -n '11,20p' {path}"})),
        ])
        self.assert_status(path, 'READ', 0)

        self.write_journal([
            assistant(use('Bash', {'command': f"sed -n '1,10p' {path}"})),
            assistant(use('Bash', {'command': f"sed -n '12,20p' {path}"})),
        ])
        self.assert_status(path, 'PART', 1)


    def content_file(self, lines=None, final='\n'):
        self.content_lines = lines if lines is not None else [f'unique source row {i}: payload-{i}' for i in range(1, 7)]
        self.content_path = os.path.join(self.tmp.name, 'Content.md')
        with open(self.content_path, 'w', encoding='utf-8', newline='') as fh:
            fh.write('\n'.join(self.content_lines) + final)
        return self.content_path

    def content_result(self, text, exit_code=0):
        return json.dumps({'chunk_id': 'fixture', 'wall_time_seconds': 0.1, 'exit_code': exit_code,
                           'original_token_count': 100, 'output': text}, ensure_ascii=False)

    def content_records(self, texts=None, command='python3 reader.py', exit_code=0):
        if texts is None:
            texts = ['\n'.join(self.content_lines) + '\n']
        return [record for i, text in enumerate(texts) for record in (
            codex_exec(command, call_id=f'content_{i}'),
            codex_output(f'content_{i}', COMPLETE_HEADER, self.content_result(text, exit_code)))]

    def check_content(self, status='READ', texts=None, interval='', command='python3 reader.py', records=None,
                      exit_code=0):
        self.write_journal(records if records is not None else self.content_records(texts, command, exit_code))
        self.assert_status(self.content_path + interval, status, 0 if status == 'READ' else 1)

    def test_content_python_reader_is_read(self):
        self.content_file()
        self.check_content()

    def test_content_command_array_is_read(self):
        self.content_file()
        records = self.content_records()
        records[0]['payload']['input'] = 'const cmds=["python3 reader.py"]; text(await tools.exec_command({cmd:cmds[0]}));'
        self.check_content(records=records)

    def test_content_command_loop_is_read(self):
        self.content_file()
        records = self.content_records()
        records[0]['payload']['input'] = 'for (const cmd of ["python3 reader.py"]) {text(await tools.exec_command({cmd}));}'
        self.check_content(records=records)

    def test_content_sed_chunks_via_loop_variable(self):
        self.content_file()
        texts = ['\n'.join(self.content_lines[:3]), '\n'.join(self.content_lines[3:])]
        records = self.content_records(texts)
        for record in records[::2]:
            record['payload']['input'] = 'for (const path of paths) {const cmd=`sed -n "${a},${b}p" ${path}`; text(await tools.exec_command({cmd}));}'
        self.check_content(records=records)

    def test_content_nl_numbered_lines_are_read(self):
        self.content_file()
        self.check_content(texts=['\n'.join(f'{i:6}\t{s}' for i, s in enumerate(self.content_lines, 1))])

    def test_content_cat_n_numbered_lines_are_read(self):
        self.content_file()
        self.check_content(texts=['\n'.join(f'     {i}\t{s}' for i, s in enumerate(self.content_lines, 1))])

    def test_content_crlf_is_read(self):
        self.content_file([f'row {i}\r' for i in range(6)])
        self.check_content(texts=['\r\n'.join(f'row {i}' for i in range(6)) + '\r\n'])

    def test_content_no_final_newline_is_read(self):
        self.content_file(final='')
        self.check_content(texts=['\n'.join(self.content_lines)])

    def test_content_final_lf_adds_no_line(self):
        self.content_file()
        self.check_content()

    def test_content_nonzero_command_exit_still_shows_text(self):
        self.content_file()
        self.check_content(exit_code=1)

    def test_content_outer_warning_excludes_output(self):
        self.content_file()
        records = self.content_records()
        records[1]['payload']['output'][0]['text'] = TRUNCATION_WARNING + COMPLETE_HEADER
        self.check_content('MISS', records=records)

    def test_content_inner_warning_excludes_output(self):
        self.content_file()
        self.check_content('MISS', texts=[TRUNCATION_WARNING + '\n'.join(self.content_lines)])

    def test_content_bytes_omitted_excludes_output(self):
        self.content_file()
        self.check_content('MISS', texts=['... 141077 bytes omitted ...\n' + '\n'.join(self.content_lines)])

    def test_content_tokens_truncated_excludes_output(self):
        self.content_file()
        self.check_content('MISS', texts=['…95 tokens truncated…\n' + '\n'.join(self.content_lines)])

    def test_legacy_cat_bytes_omitted_is_not_read(self):
        self.content_file()
        self.check_content('PART', texts=['... 141077 bytes omitted ...\n' + '\n'.join(self.content_lines)],
                           command=f'cat {self.content_path}')

    def test_legacy_cat_tokens_truncated_is_not_read(self):
        self.content_file()
        self.check_content('PART', texts=['…95 tokens truncated…\n' + '\n'.join(self.content_lines)],
                           command=f'cat {self.content_path}')

    def test_legacy_sed_tokens_truncated_is_not_read(self):
        self.content_file()
        self.check_content('PART', texts=['…95 tokens truncated…\n' + '\n'.join(self.content_lines)],
                           command=f"sed -n '1,6p' {self.content_path}")

    def test_content_missing_middle_is_part(self):
        self.content_file()
        self.check_content('PART', texts=['\n'.join(self.content_lines[:2] + self.content_lines[3:])])

    def test_content_other_file_isolated_lines_are_miss(self):
        self.content_file()
        other = os.path.join(self.tmp.name, 'Other.md')
        with open(other, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(self.content_lines[::2]))
        self.check_content('MISS', texts=['\n'.join(self.content_lines[::2])])

    def test_content_before_last_compaction_is_miss(self):
        self.content_file()
        self.check_content('MISS', records=self.content_records() + [CODEX_COMPACTION])

    def test_content_blocks_do_not_cross_compaction(self):
        self.content_file()
        records = self.content_records(['\n'.join(self.content_lines[:3]), '\n'.join(self.content_lines[3:])])
        records.insert(2, CODEX_COMPACTION)
        self.check_content('PART', records=records)

    def test_content_scattered_lines_shorter_than_k_are_miss(self):
        self.content_file()
        self.check_content('MISS', texts=['\nforeign row\n'.join(self.content_lines)])

    def test_content_short_outputs_are_not_concatenated(self):
        self.content_file()
        self.check_content('MISS', texts=['\n'.join(self.content_lines[i:i + 2]) for i in (0, 2, 4)])

    def test_content_sibling_results_are_not_concatenated(self):
        self.content_file()
        results = [self.content_result('\n'.join(self.content_lines[i:i + 2])) for i in (0, 2, 4)]
        self.check_content('MISS', records=[codex_exec('python3 reader.py', 'one'), codex_output('one', *results)])

    def test_content_text_blocks_are_not_concatenated(self):
        self.content_file()
        texts = ['\n'.join(self.content_lines[i:i + 2]) for i in (0, 2, 4)]
        self.check_content('MISS', records=[codex_exec('python3 reader.py', 'one'), codex_output('one', *texts)])

    def test_content_message_is_not_output(self):
        self.content_file()
        self.check_content('MISS', records=[{'type': 'response_item', 'payload': {
            'type': 'message', 'content': [{'type': 'output_text', 'text': '\n'.join(self.content_lines)}]}}])

    def test_content_call_input_is_not_output(self):
        self.content_file()
        self.check_content('MISS', records=[codex_exec('\n'.join(self.content_lines), 'one')])

    def test_content_event_message_is_not_output(self):
        self.content_file()
        self.check_content('MISS', records=[{'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'output': '\n'.join(self.content_lines)}}])

    def test_content_unrelated_tool_output_is_ignored(self):
        self.content_file()
        records = self.content_records()
        records[0]['payload']['name'] = 'unrelated'
        self.check_content('MISS', records=records)

    def test_content_fulfilled_result_wrapper(self):
        self.content_file()
        records = self.content_records()
        value = json.loads(records[1]['payload']['output'][1]['text'])
        records[1]['payload']['output'][1]['text'] = json.dumps({'i': 2, 'result': {'status': 'fulfilled', 'value': value}})
        self.check_content(records=records)

    def test_content_does_not_decode_printed_json_as_tool_result(self):
        self.content_file()
        printed = self.content_result('\n'.join(self.content_lines))
        self.check_content('MISS', texts=[printed])

    def test_content_comparison_is_case_sensitive(self):
        self.content_file()
        lines = self.content_lines.copy()
        lines[2] = lines[2].upper()
        self.check_content('PART', texts=['\n'.join(lines)])

    def test_content_space_number_prefix_is_not_removed(self):
        self.content_file()
        self.check_content('MISS', texts=['\n'.join(f'{i} {s}' for i, s in enumerate(self.content_lines, 1))])

    def test_content_colon_number_prefix_is_not_removed(self):
        self.content_file()
        self.check_content('MISS', texts=['\n'.join(f'{i}:{s}' for i, s in enumerate(self.content_lines, 1))])

    def test_content_source_number_prefix_via_cat(self):
        self.content_file(['1\tfoo', '2\tbar', '3\tbaz'])
        self.check_content(command='cat "$path"')

    def test_content_source_number_prefix_via_nl(self):
        self.content_file(['1\tfoo', '2\tbar', '3\tbaz'])
        self.check_content(texts=['\n'.join(f'{i}\t{s}' for i, s in enumerate(self.content_lines, 1))])

    def test_content_middle_line_checks_both_variants(self):
        self.content_file()
        lines = self.content_lines.copy()
        lines[1] = '   2\t' + lines[1]
        self.check_content(texts=['\n'.join(lines)])

    def test_content_first_edge_checks_both_variants(self):
        self.content_file()
        lines = self.content_lines.copy()
        lines[0] = '   1\t' + lines[0]
        self.check_content(texts=['\n'.join(lines)])

    def test_content_last_edge_checks_both_variants(self):
        self.content_file()
        lines = self.content_lines.copy()
        lines[-1] = '   6\t' + lines[-1]
        self.check_content(texts=['\n'.join(lines)])

    def test_content_one_line_file_requires_exact_match(self):
        self.content_file(['single unique line'])
        self.check_content()

    def test_content_one_line_suffix_is_miss(self):
        self.content_file(['single unique line'])
        self.check_content('MISS', texts=['unique line'])

    def test_content_one_line_prefix_is_miss(self):
        self.content_file(['single unique line'])
        self.check_content('MISS', texts=['single unique'])

    def test_content_two_line_file_full_output_is_read(self):
        self.content_file(['first unique line', 'second different line'])
        self.check_content()

    def test_content_two_line_file_partial_edges_are_miss(self):
        self.content_file(['first unique line', 'second different line'])
        self.check_content('MISS', texts=['unique line\nsecond different'])

    def test_content_repeated_lines_check_all_alignments(self):
        self.content_file(['repeat'] * 6)
        self.check_content(texts=['repeat\nrepeat\nrepeat'])

    def test_content_empty_line_requires_exact_block(self):
        self.content_file(['a', 'b', 'c', '', 'd', 'e', 'f'])
        self.check_content()

    def test_content_empty_line_not_covered_by_zero_length_edges(self):
        self.content_file(['a', 'b', 'c', '', 'd', 'e', 'f'])
        self.check_content('PART', texts=['a\nb\nc\nd\ne\nf'])

    def test_content_empty_file_is_not_vacuously_read(self):
        self.content_file([], final='')
        self.check_content('MISS', texts=[''])

    def test_legacy_cat_empty_file_keeps_status(self):
        self.content_file([], final='')
        self.check_content(texts=[''], command=f'cat {self.content_path}')

    def test_content_requested_read_range_is_read(self):
        self.content_file()
        self.check_content(texts=['\n'.join(self.content_lines[:3])], interval=':1-3')

    def test_content_requested_unread_range_is_miss(self):
        self.content_file()
        self.check_content('MISS', texts=['\n'.join(self.content_lines[:3])], interval=':4-6')

    def test_content_range_length_does_not_lower_k(self):
        self.content_file()
        self.check_content('MISS', texts=[self.content_lines[2]], interval=':3-3')

    def test_content_range_can_use_context_outside_range(self):
        self.content_file()
        self.check_content(texts=['\n'.join(self.content_lines[1:4])], interval=':3-3')

    def test_legacy_cat_covers_requested_range(self):
        self.content_file()
        self.check_content(texts=['legacy'], command=f'cat {self.content_path}', interval=':2-4')

    def test_legacy_sed_covers_requested_range(self):
        self.content_file()
        self.check_content(texts=['legacy'], command=f"sed -n '2,4p' {self.content_path}", interval=':2-4')

    def test_range_mention_adds_no_coverage(self):
        self.content_file()
        self.check_content('MISS', texts=['legacy'], command=f'echo {self.content_path}', interval=':2-4')

    def test_missing_source_range_is_miss(self):
        self.content_file()
        os.unlink(self.content_path)
        self.check_content('MISS', texts=['legacy'], interval=':1-3')

    def test_legacy_sed_and_content_cover_file(self):
        self.content_file()
        records = [codex_exec(f"sed -n '1,3p' {self.content_path}", 'legacy'), codex_output('legacy', 'text')]
        records += self.content_records(['\n'.join(self.content_lines[3:])])
        self.check_content(records=records)

    def test_content_zero_range_start_returns_two(self):
        self.content_file()
        self.write_journal(self.content_records())
        self.assertEqual(self.run_cli(self.content_path + ':0-3').returncode, 2)

    def test_content_reversed_range_returns_two(self):
        self.content_file()
        self.write_journal(self.content_records())
        self.assertEqual(self.run_cli(self.content_path + ':4-2').returncode, 2)

    def test_content_range_past_eof_returns_two(self):
        self.content_file()
        self.write_journal(self.content_records())
        self.assertEqual(self.run_cli(self.content_path + ':1-7').returncode, 2)

    def test_content_ranges_of_same_file_are_independent(self):
        self.content_file()
        self.write_journal(self.content_records(['\n'.join(self.content_lines[:3])]))
        result = self.run_cli(self.content_path + ':1-3', self.content_path + ':4-6')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f'READ {self.content_path}:1-3', f'MISS {self.content_path}:4-6'])

    def split_fixture(self, unicode=False):
        lines = ['first long context row AAA', 'second long context row BBB',
                 'начало😀середина🦊окончание' if unicode else 'START-abcdefghij-END',
                 'fourth long context row DDD', 'fifth long context row EEE']
        self.content_file(lines)
        cut = len('начало😀'.encode()) if unicode else 10
        data = lines[2].encode()
        self.split_prefix = data[:cut].decode()
        self.split_suffix = data[cut:].decode()
        size = len(('\n'.join(lines[:2]) + '\n').encode()) + cut
        total = len(('\n'.join(lines) + '\n').encode())
        self.split_headers = [f'{self.content_path} lines 1-5, bytes 0:{size}, chunk 1/2',
                              f'{self.content_path} lines 1-5, bytes {size}:{total}, chunk 2/2']
        return self.split_outputs(self.split_prefix, self.split_suffix)

    def split_outputs(self, prefix, suffix):
        lines = self.content_lines
        return ['\n'.join([self.split_headers[0], *lines[:2], prefix]),
                '\n'.join([self.split_headers[1], suffix, *lines[3:]])]

    def test_content_python_byte_split_is_read(self):
        texts = self.split_fixture()
        self.check_content(texts=texts)

    def test_content_split_line_range_is_read(self):
        texts = self.split_fixture()
        self.check_content(texts=texts, interval=':3-3')

    def test_content_split_edges_may_overlap(self):
        self.split_fixture()
        self.check_content(texts=self.split_outputs(self.content_lines[2][:12], self.content_lines[2][8:]), interval=':3-3')

    def test_content_middle_fragment_cannot_cover_line(self):
        self.split_fixture()
        middle = self.content_lines[2][3:-3]
        self.check_content('MISS', texts=self.split_outputs(middle, middle), interval=':3-3')

    def test_content_suffix_without_prefix_is_miss(self):
        texts = self.split_fixture()
        self.check_content('MISS', texts=texts[1:], interval=':3-3')

    def test_content_suffix_neighbors_must_be_consecutive(self):
        texts = self.split_fixture()
        texts[1] = texts[1].replace(self.content_lines[3], self.content_lines[0])
        self.check_content('MISS', texts=texts, interval=':3-3')

    def test_content_first_fragment_must_be_true_suffix(self):
        self.split_fixture()
        self.check_content('MISS', texts=self.split_outputs(self.split_prefix, 'false suffix'), interval=':3-3')

    def test_content_split_edges_with_gap_are_miss(self):
        self.split_fixture()
        self.check_content('MISS', texts=self.split_outputs(self.split_prefix[:-1], self.split_suffix), interval=':3-3')

    def test_content_duplicate_prefixes_do_not_add_lengths(self):
        self.split_fixture()
        second = self.content_lines[2][:12]
        texts = self.split_outputs(self.split_prefix, self.split_suffix)
        texts[1] = '\n'.join([self.split_headers[1], *self.content_lines[:2], second])
        self.check_content('MISS', texts=texts, interval=':3-3')

    def test_content_duplicate_suffixes_do_not_add_lengths(self):
        self.split_fixture()
        first = self.content_lines[2][8:]
        texts = self.split_outputs(self.split_prefix, self.split_suffix)
        texts[0] = '\n'.join([self.split_headers[0], first, *self.content_lines[3:]])
        self.check_content('MISS', texts=texts, interval=':3-3')

    def test_content_truncated_prefix_cannot_complete_suffix(self):
        texts = self.split_fixture()
        texts[0] = TRUNCATION_WARNING + texts[0]
        self.check_content('MISS', texts=texts, interval=':3-3')

    def test_content_split_edges_reset_at_compaction(self):
        texts = self.split_fixture()
        records = self.content_records(texts)
        records.insert(2, CODEX_COMPACTION)
        self.check_content('MISS', records=records, interval=':3-3')

    def test_content_split_edge_requires_full_block_length(self):
        texts = self.split_fixture()
        texts[0] = texts[0].replace(self.content_lines[0] + '\n', '')
        self.check_content('MISS', texts=texts, interval=':3-3')

    def test_content_split_edges_use_utf8_byte_lengths(self):
        texts = self.split_fixture(unicode=True)
        self.check_content(texts=texts)

    def unicode_fixture(self, inside=False):
        self.content_file(['left one', 'left two', 'left three', 'начало😀окончание',
                           'right five', 'right six', 'right seven'])
        data = self.content_lines[3].encode()
        cut = len('начало'.encode()) + (1 if inside else 0)
        return ['header chunk 1/2\n' + '\n'.join([*self.content_lines[:3], data[:cut].decode(errors='replace')]),
                'header chunk 2/2\n' + '\n'.join([data[cut:].decode(errors='replace'), *self.content_lines[4:]])]

    def test_content_unicode_boundary_split_is_read(self):
        texts = self.unicode_fixture()
        self.check_content(texts=texts)

    def test_content_split_inside_utf8_character_is_part(self):
        texts = self.unicode_fixture(inside=True)
        self.check_content('PART', texts=texts)


    def test_windows_drive_path_with_hyphen_is_not_a_range(self):
        path = r'C:\Work\some-rules.md'
        self.write_journal([codex_exec(f'cat "{path}"')])
        self.assert_status(path, 'READ', 0)

    def test_content_malformed_range_returns_two(self):
        self.content_file()
        self.write_journal(self.content_records())
        self.assertEqual(self.run_cli(self.content_path + ':1-x').returncode, 2)

    def test_content_plain_string_output_is_read(self):
        self.content_file()
        record = codex_output('plain')
        record['payload']['output'] = '\n'.join(self.content_lines)
        self.check_content(records=[codex_exec('python3 reader.py', 'plain'), record])

    def test_content_untruncated_sibling_survives_inner_truncation(self):
        self.content_file()
        good = self.content_result('\n'.join(self.content_lines))
        cut = self.content_result('…95 tokens truncated…')
        self.check_content(records=[codex_exec('python3 reader.py', 'siblings'),
                                    codex_output('siblings', cut, good)])

    def test_legacy_cat_truncated_sibling_invalidates_whole_call(self):
        self.content_file()
        cut = self.content_result('…95 tokens truncated…')
        plain = self.content_result('legacy')
        self.check_content('PART', records=[codex_exec(f'cat {self.content_path}', 'siblings'),
                                            codex_output('siblings', cut, plain)])

    def test_missing_source_range_can_use_legacy_sed(self):
        self.content_file()
        os.unlink(self.content_path)
        self.check_content(texts=['legacy'], command=f"sed -n '1,3p' {self.content_path}", interval=':1-3')

    def test_range_partial_coverage_is_part(self):
        self.content_file()
        self.check_content('PART', texts=['\n'.join(self.content_lines[:3])], interval=':2-4')


    def test_legacy_sed_keeps_byte_line_count_for_non_utf8_source(self):
        path = os.path.join(self.tmp.name, 'bytes.md')
        with open(path, 'wb') as fh:
            fh.write(b'first\n\xff\nlast\n')
        self.write_journal(self.sed_journal(path, '1,3p'))
        self.assert_status(path, 'READ', 0)

    def test_range_past_non_utf8_source_eof_returns_two(self):
        path = os.path.join(self.tmp.name, 'bytes.md')
        with open(path, 'wb') as fh:
            fh.write(b'first\n\xff\nlast\n')
        self.write_journal(self.sed_journal(path, '1,3p'))
        self.assertEqual(self.run_cli(path + ':1-4').returncode, 2)


if __name__ == '__main__':
    unittest.main()
