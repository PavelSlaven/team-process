import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))
WATCH = os.path.join(TOOLS, 'codex-watch.py')
LITERAL = os.path.join(TOOLS, 'check-literal.py')
LEAD_ID = '019aaaaa-lead'


def run(script, *args):
    p = subprocess.run([sys.executable, '-I', script, *args], capture_output=True, encoding='utf-8')
    return p.returncode, p.stdout, p.stderr


def stamp(minutes_ago):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return t.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def cmd_rec(minutes_ago, command, exit_code='0', output='ok'):
    item = {'type': 'CommandExecution', 'command': command, 'exit_code': exit_code, 'aggregated_output': output}
    return {'timestamp': stamp(minutes_ago), 'type': 'event_msg',
            'payload': {'type': 'item_completed', 'item': item}}


def says_rec(minutes_ago, text):
    return {'timestamp': stamp(minutes_ago), 'type': 'response_item',
            'payload': {'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': text}]}}


def msg_rec(minutes_ago, text):
    return {'timestamp': stamp(minutes_ago), 'type': 'response_item',
            'payload': {'type': 'agent_message', 'author': '/root/x', 'recipient': '/root',
                        'content': [{'type': 'input_text', 'text': text}]}}


def write_log(path, records, first=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        if first:
            fh.write(json.dumps(first, separators=(',', ':')) + '\n')
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')


class CodexWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = os.path.join(self.tmp.name, 'sessions', '2026', '10', '28')
        self.out = os.path.join(self.tmp.name, 'out')
        write_log(os.path.join(self.sessions, f'rollout-2026-10-28T10-00-00-{LEAD_ID}.jsonl'), [
            says_rec(5, 'lead says hello'),
            cmd_rec(4, 'git status'),
            {'timestamp': stamp(3), 'type': 'compacted', 'payload': {}},
        ] + [cmd_rec(2, 'npm test', '0') for _ in range(5)] + [cmd_rec(1, 'false', '1', 'boom')])
        write_log(os.path.join(self.sessions, 'rollout-2026-10-28T10-01-00-019bbbbb-helper.jsonl'), [
            says_rec(5, 'helper says'),
            msg_rec(4, 'report to lead'),
            cmd_rec(3, 'ls'),
            cmd_rec(200, 'too old command'),
        ], first={'timestamp': stamp(250), 'type': 'session_meta',
                  'payload': {'parent_thread_id': LEAD_ID, 'agent_nickname': 'Kepler'}})
        write_log(os.path.join(self.sessions, 'rollout-2026-10-28T10-02-00-019ccccc-other.jsonl'), [
            cmd_rec(3, 'unrelated'),
        ], first={'timestamp': stamp(250), 'type': 'session_meta',
                  'payload': {'parent_thread_id': 'someone-else', 'agent_nickname': 'Stranger'}})

    def watch(self, *extra):
        rc, out, err = run(WATCH, '--thread', LEAD_ID, '--minutes', '60',
                           '--sessions', os.path.join(self.tmp.name, 'sessions'), *extra)
        self.assertEqual(rc, 0, err)
        return out

    def test_helper_found_by_parent_thread_id(self):
        out = self.watch()
        self.assertIn('[Kepler]', out)
        self.assertIn('[LEAD]', out)
        self.assertNotIn('Stranger', out)

    def test_nested_helper_found_by_root_session_id(self):
        write_log(os.path.join(self.sessions, 'rollout-2026-10-28T10-03-00-019ddddd-grandchild.jsonl'), [
            cmd_rec(2, 'pair test'),
        ], first={'timestamp': stamp(250), 'type': 'session_meta',
                  'payload': {'session_id': LEAD_ID, 'parent_thread_id': '019bbbbb-helper',
                              'agent_nickname': 'Banach'}})
        self.assertIn('[Banach]', self.watch())

    def test_live_helper_with_stale_mtime_found(self):
        path = os.path.join(self.sessions, 'rollout-2026-10-28T10-04-00-019eeeee-open.jsonl')
        write_log(path, [cmd_rec(2, 'still writing')], first={'timestamp': stamp(250), 'type': 'session_meta',
                  'payload': {'session_id': LEAD_ID, 'parent_thread_id': LEAD_ID, 'agent_nickname': 'Noether'}})
        old = (dt.datetime.now() - dt.timedelta(hours=3)).timestamp()
        os.utime(path, (old, old))
        self.assertIn('[Noether]', self.watch())

    def test_helper_without_records_in_window_skipped(self):
        write_log(os.path.join(self.sessions, 'rollout-2026-10-28T10-05-00-019fffff-done.jsonl'),
                  [cmd_rec(200, 'long ago')], first={'timestamp': stamp(250), 'type': 'session_meta',
                  'payload': {'parent_thread_id': LEAD_ID, 'agent_nickname': 'Hypatia'}})
        self.assertNotIn('Hypatia', self.watch())

    def test_file_change_in_timeline(self):
        rec = {'timestamp': stamp(2), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'FileChange', 'changes': {r'C:\wt\tools\check.py': {
                'type': 'update', 'unified_diff': '@@ -1 +1 @@\n-    pass\n+    return 1'}}}}}
        write_log(os.path.join(self.sessions, 'rollout-2026-10-28T10-06-00-019abcde-editor.jsonl'), [rec],
                  first={'timestamp': stamp(250), 'type': 'session_meta',
                         'payload': {'session_id': LEAD_ID, 'agent_nickname': 'Hopper'}})
        out = self.watch('--out', self.out)
        self.assertIn('edits 1;', out.split('[Hopper]')[1])
        with open(os.path.join(self.out, 'Hopper.txt'), encoding='utf-8') as fh:
            self.assertIn('EDIT update check.py:', fh.read())

    def test_repeat_and_fail_lines(self):
        out = self.watch()
        self.assertIn('REPEAT x5 (identical): npm test', out)
        self.assertIn('fail ', out)
        self.assertIn('exit 1: false', out)

    def test_compactions_counted(self):
        self.assertIn('compactions 1', self.watch())

    def test_out_writes_timelines(self):
        self.watch('--out', self.out)
        self.assertEqual(sorted(os.listdir(self.out)), ['Kepler.txt', 'LEAD.txt'])
        with open(os.path.join(self.out, 'LEAD.txt'), encoding='utf-8') as fh:
            lead = fh.read()
        for word in ('SAYS: lead says hello', 'CMD exit 0: git status', 'COMPACTED'):
            self.assertIn(word, lead)
        with open(os.path.join(self.out, 'Kepler.txt'), encoding='utf-8') as fh:
            helper = fh.read()
        self.assertIn('MSG /root/x -> /root: report to lead', helper)
        self.assertNotIn('too old command', helper)

    def test_no_out_no_files(self):
        self.watch()
        self.assertFalse(os.path.exists(self.out))

    def test_old_records_excluded_from_counts(self):
        out = self.watch()
        helper = out.split('[Kepler]')[1].split('\n\n')[0]
        self.assertIn('commands 1;', helper)

    def test_worktree_section_only_with_flag(self):
        self.assertNotIn('worktree:', self.watch())
        repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(repo)
        subprocess.run(['git', '-C', repo, 'init', '-q'], check=True)
        with open(os.path.join(repo, 'a.txt'), 'w') as fh:
            fh.write('x')
        out = self.watch('--worktree', repo)
        self.assertIn('worktree: 1 changed paths; touched in window: 1', out)
        self.assertIn('  a.txt', out)


class CheckLiteralTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def check(self, text, *flags):
        path = os.path.join(self.tmp.name, 'doc.md')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        rc, out, err = run(LITERAL, *flags, path)
        self.assertIn(rc, (0, 1), err)
        return rc, out

    def test_vague_flagged(self):
        rc, out = self.check('Сделай по возможности быстро.\n')
        self.assertEqual(rc, 1)
        self.assertIn(':1: РАСПЛЫВЧАТО: по возможности — ', out)
        self.assertIn('флагов: 1', out)

    def test_vague_case_insensitive_whole_word(self):
        self.assertEqual(self.check('Желательно так.\n')[0], 1)
        self.assertEqual(self.check('Это лучшее решение и достаточные меры.\n')[0], 0)
        self.assertEqual(self.check('Список etc.\n')[0], 1)

    def test_clean_text_silent(self):
        rc, out = self.check('Запусти три теста и сверь счётчики.\n')
        self.assertEqual(rc, 0)
        self.assertIn('флагов: 0', out)

    def test_code_block_inline_code_and_quote_skipped(self):
        text = '```\nпо возможности\n```\nвот `при необходимости` код\n> по желанию цитата\nчисто\n'
        rc, out = self.check(text)
        self.assertEqual(rc, 0, out)

    def test_limit_flagged_without_launch_words(self):
        rc, out = self.check('Не больше 40 помощников.\n')
        self.assertEqual(rc, 1)
        self.assertIn('ПРЕДЕЛ: ', out)
        self.assertIn('назван только предел', out)

    def test_limit_with_launch_words_silent(self):
        rc, _ = self.check('До 60 помощников одновременно, независимое запускай сразу.\n')
        self.assertEqual(rc, 0)

    def test_limit_variants(self):
        self.assertEqual(self.check('максимум 12 субагентов\n')[0], 1)
        self.assertEqual(self.check('не более 5 исполнителей\n')[0], 1)

    SECTION = ('# Задание\n\n## Организация работы\n\n- Порядок: сначала А.\n- **Параллельно**: Б.\n'
               '- Помощники: до 3 одновременно, запускай сразу.\n- Остановки: после А.\n- Рамка правок: только X.\n\n## Дальше\n')

    def test_task_without_section_flagged(self):
        rc, out = self.check('# Задание\n\nТекст.\n', '--task')
        self.assertEqual(rc, 1)
        self.assertIn('РАЗДЕЛ: ', out)
        self.assertIn('нужен раздел «Организация работы»', out)

    def test_task_with_full_section_silent(self):
        rc, out = self.check(self.SECTION, '--task')
        self.assertEqual(rc, 0, out)

    def test_task_missing_one_item_flagged_once(self):
        rc, out = self.check(self.SECTION.replace('- Остановки: после А.\n', ''), '--task')
        self.assertEqual(rc, 1)
        self.assertEqual(out.count('РАЗДЕЛ'), 1)
        self.assertIn('Остановки', out)

    def test_section_ends_at_same_level_heading(self):
        text = self.SECTION.replace('- Остановки: после А.\n', '').replace('## Дальше\n', '## Дальше\n- Остановки: тут.\n')
        rc, out = self.check(text, '--task')
        self.assertEqual(out.count('РАЗДЕЛ'), 1)

    def test_section_not_checked_without_task_flag(self):
        self.assertEqual(self.check('Текст.\n')[0], 0)


if __name__ == '__main__':
    unittest.main()
