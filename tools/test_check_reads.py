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


if __name__ == '__main__':
    unittest.main()
