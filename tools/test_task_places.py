import os
import stat
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))
PLACES = os.path.join(TOOLS, 'task-places.py')


def run(*args):
    p = subprocess.run([sys.executable, '-I', PLACES, *args], capture_output=True, encoding='utf-8',
                       stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout, p.stderr


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                          check=True, capture_output=True, encoding='utf-8', stdin=subprocess.DEVNULL).stdout


def rows(out):
    lines = out.strip().splitlines()
    head = lines[0].split('\t')
    return {r.split('\t')[0]: dict(zip(head, r.split('\t'))) for r in lines[1:]}


class TaskPlaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.origin = os.path.join(t, 'origin.git')
        self.main = os.path.join(t, 'main')
        self.wt = os.path.join(t, 'task-7', 'wt')
        self.archive = os.path.join(t, 'archive')
        self.refs = os.path.join(t, 'refs')
        os.makedirs(self.refs)
        subprocess.run(['git', 'init', '-q', '--bare', self.origin], check=True, stdin=subprocess.DEVNULL)
        subprocess.run(['git', 'clone', '-q', self.origin, self.main], check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
        with open(os.path.join(self.main, 'a.txt'), 'w') as fh:
            fh.write('one\n')
        git(self.main, 'add', 'a.txt')
        git(self.main, 'commit', '-q', '-m', 'one')
        git(self.main, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        git(self.main, 'worktree', 'add', '-q', '-b', 'task-7', self.wt)
        with open(os.path.join(self.wt, 'b.txt'), 'w') as fh:
            fh.write('unpushed\n')
        git(self.wt, 'add', 'b.txt')
        git(self.wt, 'commit', '-q', '-m', 'unpushed')
        with open(os.path.join(self.wt, 'a.txt'), 'a') as fh:
            fh.write('dirty\n')
        with open(os.path.join(self.wt, 'new.txt'), 'w') as fh:
            fh.write('untracked\n')

    def tearDown(self):
        self.tmp.cleanup()

    def norm(self, p):
        p = os.path.abspath(p).replace('\\', '/')
        return p.lower() if os.name == 'nt' else p

    def test_inventory_counts_worktree_state(self):
        rc, out, err = run('inventory', self.wt, self.main)
        self.assertEqual(rc, 0, err)
        r = rows(out)
        wt = r[self.norm(self.wt)]
        self.assertEqual((wt['kind'], wt['dirty'], wt['untracked'], wt['unpushed']),
                         ('worktree', '1', '1', '1'))
        self.assertEqual(wt['branch'], 'task-7')
        self.assertRegex(wt['head_date'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(r[self.norm(self.main)]['kind'], 'repo')
        self.assertEqual(r[self.norm(self.main)]['worktrees'], '1')

    def test_inventory_finds_reference_by_folder_name(self):
        with open(os.path.join(self.refs, 'plan.md'), 'w', encoding='utf-8') as fh:
            fh.write('see scratch\\task-7\\wt for logs\n')
        rc, out, err = run('inventory', self.wt, '--refs', self.refs)
        self.assertEqual(rc, 0, err)
        self.assertIn('plan.md:1', rows(out)[self.norm(self.wt)]['refs'])

    def test_inventory_searches_refs_for_path_of_another_machine(self):
        with open(os.path.join(self.refs, 'goal.md'), 'w', encoding='utf-8') as fh:
            fh.write('W=/srv/work/scratch/task-31/repo\n')
        rc, out, err = run('inventory', '/srv/work/scratch/task-31/repo', '--refs', self.refs)
        self.assertEqual(rc, 0, err)
        row = next(iter(rows(out).values()))
        self.assertEqual(row['kind'], 'missing')
        self.assertIn('goal.md:1', row['refs'])

    def test_plain_word_folder_name_alone_is_not_a_reference(self):
        with open(os.path.join(self.refs, 'notes.md'), 'w', encoding='utf-8') as fh:
            fh.write('the Novgorod repository\n')
        rc, out, err = run('inventory', '/srv/work/task-9/Novgorod', '--refs', self.refs)
        self.assertEqual(rc, 0, err)
        self.assertEqual(next(iter(rows(out).values()))['refs'], '0')

    def test_remove_archives_changes_then_removes_worktree(self):
        rc, out, err = run('remove', self.wt, '--refs', self.refs, '--archive', self.archive)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.wt))
        self.assertNotIn('task-7/wt', git(self.main, 'worktree', 'list').replace('\\', '/'))
        names = os.listdir(self.archive)
        for suffix in ('.head', '.diff', '.untracked.tar', '.bundle'):
            self.assertTrue(any(n.endswith(suffix) for n in names), (suffix, names))
        diff = next(n for n in names if n.endswith('.diff'))
        with open(os.path.join(self.archive, diff), encoding='utf-8') as fh:
            self.assertIn('+dirty', fh.read())
        bundle = os.path.join(self.archive, next(n for n in names if n.endswith('.bundle')))
        git(self.main, 'bundle', 'verify', bundle)
        with open(os.path.join(self.archive, 'remove.log'), encoding='utf-8') as fh:
            self.assertIn('removed', fh.read())

    def test_remove_refuses_referenced_place(self):
        with open(os.path.join(self.refs, 'goal.md'), 'w', encoding='utf-8') as fh:
            fh.write(f'W={self.wt}\n')
        rc, out, err = run('remove', self.wt, '--refs', self.refs, '--archive', self.archive)
        self.assertNotEqual(rc, 0)
        self.assertTrue(os.path.exists(self.wt))
        self.assertIn('goal.md:1', err)

    @unittest.skipUnless(os.path.isdir('/proc'), 'process check reads /proc')
    def test_remove_refuses_place_with_running_process(self):
        proc = subprocess.Popen(['sleep', '60'], cwd=self.wt)
        try:
            rc, out, err = run('remove', self.wt, '--refs', self.refs, '--archive', self.archive)
        finally:
            proc.kill()
            proc.wait()
        self.assertNotEqual(rc, 0)
        self.assertIn('processes', err)
        self.assertTrue(os.path.exists(self.wt))

    def test_remove_refuses_repo_with_linked_worktrees(self):
        rc, out, err = run('remove', self.main, '--refs', self.refs, '--archive', self.archive)
        self.assertNotEqual(rc, 0)
        self.assertIn('linked worktrees', err)
        self.assertTrue(os.path.exists(os.path.join(self.main, 'a.txt')))

    def test_remove_plain_dir_with_read_only_file(self):
        d = os.path.join(self.tmp.name, 'task-7', 'build')
        os.makedirs(d)
        f = os.path.join(d, 'obj.o')
        with open(f, 'w') as fh:
            fh.write('x')
        os.chmod(f, stat.S_IREAD)
        rc, out, err = run('remove', d, '--refs', self.refs, '--archive', self.archive)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(d))

    def test_dry_run_changes_nothing(self):
        rc, out, err = run('remove', self.wt, '--refs', self.refs, '--archive', self.archive, '--dry-run')
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.wt, 'new.txt')))
        self.assertFalse(os.path.exists(self.archive))

    def test_remove_requires_refs(self):
        rc, out, err = run('remove', self.wt, '--archive', self.archive)
        self.assertNotEqual(rc, 0)
        self.assertIn('--refs', err)
        self.assertTrue(os.path.exists(self.wt))


if __name__ == '__main__':
    unittest.main()
