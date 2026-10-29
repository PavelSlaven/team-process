"""Inventory and remove a finished task's work places: worktrees, clones, build trees, temp dirs.

inventory PATH... [--refs P...] [--size]    one TSV row per place, read-only
remove PATH... --refs P... --archive DIR    archive unsaved git state, then delete
"""
import argparse
import datetime as dt
import functools
import os
import shutil
import stat
import subprocess
import sys
import tarfile

COLUMNS = ['path', 'kind', 'branch', 'head_date', 'size', 'dirty', 'untracked', 'unpushed', 'worktrees',
           'procs', 'refs']
UNTRACKED_LIMIT = 100 * 1024 * 1024
TEXT_LIMIT = 2 * 1024 * 1024


def norm(p):
    # A path of another machine is kept as written, so its references can be checked here.
    p = p.replace('\\', '/') if p.startswith('/') else os.path.abspath(p).replace('\\', '/')
    return p.lower() if os.name == 'nt' else p


def git(path, *args):
    p = subprocess.run(['git', '-C', path, *args], capture_output=True, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def kind_of(path):
    if not os.path.isdir(path):
        return 'missing'
    dot = os.path.join(path, '.git')
    if os.path.isfile(dot):
        return 'worktree'
    return 'repo' if os.path.isdir(dot) else 'dir'


def size_of(path):
    # scandir entries carry the directory listing's stat on Windows, so no file is opened.
    total, stack = 0, [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def procs_in(path):
    if not os.path.isdir('/proc'):
        return '?'
    n, root = 0, norm(path)
    for pid in os.listdir('/proc'):
        try:
            cwd = norm(os.readlink(f'/proc/{pid}/cwd'))
        except OSError:
            continue
        n += cwd == root or cwd.startswith(root + '/')
    return str(n)


def needles(path):
    p = norm(path)
    parts = p.rstrip('/').split('/')
    out = {p, '/'.join(parts[-2:])}
    # Names like "repo" or "Novgorod" occur everywhere; only long names with a digit or separator are searched alone.
    name = parts[-1]
    if len(name) >= 8 and not name.isalpha():
        out.add(name)
    return out


def ref_files(refs):
    for r in refs:
        if os.path.isfile(r):
            yield r
            continue
        for top, dirs, files in os.walk(r):
            dirs[:] = [d for d in dirs if d != '.git']
            for f in files:
                yield os.path.join(top, f)


@functools.lru_cache(maxsize=None)
def ref_texts(refs):
    texts = []
    for f in ref_files(refs):
        try:
            if os.path.getsize(f) > TEXT_LIMIT:
                continue
            data = open(f, 'rb').read()
        except OSError:
            continue
        if b'\0' in data[:8192]:
            continue
        text = data.decode('utf-8', 'replace').replace('\\\\', '/').replace('\\', '/')
        if os.name == 'nt':
            text = text.lower()
        texts.append((f, norm(f), text))
    return texts


def refs_to(path, refs):
    found, own = [], norm(path) + '/'
    keys = needles(path)
    for f, nf, text in ref_texts(tuple(refs)):
        if nf.startswith(own) or not any(k in text for k in keys):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(k in line for k in keys):
                found.append(f'{f}:{i}')
                break
    return found


def describe(path, refs, with_size):
    row = dict.fromkeys(COLUMNS, '-')
    row['path'], row['kind'] = norm(path), kind_of(path)
    found = refs_to(path, refs) if refs else []
    row['refs'] = ','.join(found[:3]) + (f',+{len(found) - 3}' if len(found) > 3 else '') if found else '0'
    if row['kind'] == 'missing':
        return row, found
    if with_size:
        row['size'] = str(size_of(path))
    if row['kind'] in ('worktree', 'repo'):
        _, status = git(path, 'status', '--porcelain', '--untracked-files=all')
        lines = status.splitlines()
        row['untracked'] = str(sum(ln.startswith('??') for ln in lines))
        row['dirty'] = str(len(lines) - int(row['untracked']))
        rc, count = git(path, 'rev-list', '--count', 'HEAD', '--not', '--remotes')
        row['unpushed'] = count.strip() if rc == 0 else '0'
        row['branch'] = git(path, 'branch', '--show-current')[1].strip() or 'detached'
        row['head_date'] = git(path, 'log', '-1', '--format=%cs')[1].strip() or '-'
        if row['kind'] == 'repo':
            _, wl = git(path, 'worktree', 'list', '--porcelain')
            row['worktrees'] = str(max(0, wl.count('worktree ') - 1))
    row['procs'] = procs_in(path)
    return row, found


def refusal(path, row, found):
    p = row['path']
    if row['kind'] == 'missing':
        return 'no such directory'
    if p.rstrip('/') in (norm(os.path.expanduser('~')), norm(os.sep)) or len(p.rstrip('/').split('/')) < 3:
        return 'too close to the filesystem root'
    if row['worktrees'] not in ('-', '0'):
        return f"repository has {row['worktrees']} linked worktrees"
    if row['procs'] not in ('?', '0'):
        return f"{row['procs']} processes have their working directory inside"
    if found:
        return 'referenced by ' + ', '.join(found)
    if row['kind'] in ('worktree', 'repo') and not git(path, 'remote')[1].strip():
        return 'no remote, so the unpushed history has no bound'
    if row['untracked'] not in ('-', '0'):
        _, out = git(path, 'ls-files', '--others', '--exclude-standard', '-z')
        big = sum(os.lstat(os.path.join(path, f)).st_size for f in out.split('\0') if f)
        if big > UNTRACKED_LIMIT:
            return f'untracked files take {big} bytes, more than the archive allows'
    return None


def archive_git(path, row, archive):
    base = os.path.join(archive, ''.join(c if c.isalnum() else '_' for c in row['path']).strip('_'))
    saved = []
    _, head = git(path, 'rev-parse', 'HEAD')
    _, branch = git(path, 'branch', '--show-current')
    with open(base + '.head', 'w', encoding='utf-8') as fh:
        fh.write(f'{head.strip()} {branch.strip()}\n')
    saved.append('head')
    if row['dirty'] != '0':
        p = subprocess.run(['git', '-C', path, 'diff', '--binary', 'HEAD'], capture_output=True,
                           stdin=subprocess.DEVNULL, check=True)
        with open(base + '.diff', 'wb') as fh:
            fh.write(p.stdout)
        saved.append('diff')
    if row['untracked'] != '0':
        _, out = git(path, 'ls-files', '--others', '--exclude-standard', '-z')
        with tarfile.open(base + '.untracked.tar', 'w') as tar:
            for f in filter(None, out.split('\0')):
                tar.add(os.path.join(path, f), arcname=f)
        saved.append('untracked')
    if row['unpushed'] != '0':
        subprocess.run(['git', '-C', path, 'bundle', 'create', base + '.bundle', 'HEAD', '--not', '--remotes'],
                       capture_output=True, stdin=subprocess.DEVNULL, check=True)
        saved.append('bundle')
    return saved


def delete(path, row):
    if row['kind'] == 'worktree':
        _, common = git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        common = common.strip()
        main = os.path.dirname(common) if os.path.basename(common) == '.git' else common
        flag = [] if os.path.basename(common) == '.git' else ['--git-dir', common]
        for args in (['worktree', 'remove', '--force', path], ['worktree', 'prune']):
            p = subprocess.run(['git', *flag, '-C', main, *args], capture_output=True, stdin=subprocess.DEVNULL)
            if p.returncode:
                raise RuntimeError(p.stderr.decode('utf-8', 'replace').strip())
        return

    def unlock(func, p, *_):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=unlock)
    else:
        shutil.rmtree(path, onerror=unlock)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['inventory', 'remove'])
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--refs', nargs='+', default=[], help='live files or folders that must not point at a removed place')
    ap.add_argument('--size', action='store_true', help='sum file sizes (slow on large trees)')
    ap.add_argument('--archive', help='folder for unsaved git state and the removal log')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    if a.mode == 'inventory':
        print('\t'.join(COLUMNS))
        for p in a.paths:
            print('\t'.join(describe(p, a.refs, a.size)[0][c] for c in COLUMNS))
        return 0
    if not a.refs or not a.archive:
        print('remove needs --refs and --archive', file=sys.stderr)
        return 2
    plan = []
    for p in a.paths:
        row, found = describe(p, a.refs, False)
        why = refusal(p, row, found)
        if why:
            print(f'refused {row["path"]}: {why}', file=sys.stderr)
            return 3
        plan.append((p, row))
    for p, row in plan:
        if a.dry_run:
            print(f"would remove {row['path']} ({row['kind']}, dirty {row['dirty']}, "
                  f"untracked {row['untracked']}, unpushed {row['unpushed']})")
            continue
        os.makedirs(a.archive, exist_ok=True)
        saved = archive_git(p, row, a.archive) if row['kind'] in ('worktree', 'repo') else []
        try:
            delete(p, row)
        except (OSError, RuntimeError) as e:
            print(f"failed {row['path']} after saving {'+'.join(saved) or 'nothing'}: {e}", file=sys.stderr)
            return 4
        line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} removed {row['path']} {row['kind']} saved={'+'.join(saved) or '-'}"
        with open(os.path.join(a.archive, 'remove.log'), 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
