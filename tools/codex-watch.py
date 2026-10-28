"""Look at a Codex lead thread and its helpers: activity, repeats, failures, compactions."""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys


def parse_ts(d):
    return dt.datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00'))


def norm(cmd):
    cmd = re.sub(r'\s+', ' ', cmd)
    i = cmd.find("'-Command', ")
    return cmd[i + 12:] if i >= 0 else cmd


def head_of(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read(3000)


def scan(path, since):
    m = re.search(r'"agent_nickname":"([^"]*)"', head_of(path))
    name = m.group(1) if m else 'LEAD'
    cmds, fails, compactions, msgs, last = collections.Counter(), [], 0, [], None
    spawns, timeline = 0, []
    for line in open(path, encoding='utf-8', errors='replace'):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if 'timestamp' not in d:
            continue
        t = parse_ts(d)
        last = max(last or t, t)
        if t < since:
            continue
        p = d.get('payload') or {}
        hm = t.strftime('%H:%M:%S')
        if d['type'] == 'compacted':
            compactions += 1
            timeline.append(f'{hm} COMPACTED')
        if p.get('type') == 'item_completed':
            it = p.get('item') or {}
            if it.get('type') == 'CommandExecution':
                c = norm(str(it.get('command')))
                cmds[c] += 1
                outp = re.sub(r'\s+', ' ', str(it.get('aggregated_output') or ''))
                timeline.append(f"{hm} CMD exit {it.get('exit_code')}: {c[:600]}\n    OUT: {outp[:400]}")
                if str(it.get('exit_code')) not in ('0', 'None'):
                    fails.append((t.strftime('%H:%M'), it.get('exit_code'), c[:100]))
            if it.get('type') == 'CollabAgentToolCall':
                spawns += 1
        if p.get('type') == 'message' and p.get('role') == 'assistant':
            text = ' '.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict))
            msgs.append((t.strftime('%H:%M'), re.sub(r'\s+', ' ', text)[:220]))
            timeline.append(f'{hm} SAYS: {text[:2500]}')
        if p.get('type') == 'agent_message':
            text = ' '.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict))
            timeline.append(f"{hm} MSG {p.get('author')} -> {p.get('recipient')}: {text[:2500]}")
    return dict(name=name, path=os.path.basename(path), last=last, cmds=cmds, fails=fails,
                compactions=compactions, msgs=msgs, spawns=spawns, timeline=timeline)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--thread', required=True, help='id of the Codex lead thread')
    ap.add_argument('--minutes', type=int, default=60)
    ap.add_argument('--worktree')
    ap.add_argument('--out')
    ap.add_argument('--sessions', default=os.path.expanduser('~/.codex/sessions'))
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')

    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(minutes=a.minutes)
    # NTFS does not refresh the modified time of a file that stays open for writing,
    # so a live rollout can look hours old; the window is checked on its records instead.
    files = [f for f in glob.glob(os.path.join(a.sessions, '**', '*.jsonl'), recursive=True)
             if dt.datetime.fromtimestamp(os.path.getmtime(f), dt.timezone.utc) >= since - dt.timedelta(days=2)]
    lead_files = [f for f in files if a.thread in os.path.basename(f)]
    helpers = [f for f in files if a.thread not in os.path.basename(f)
               and (f'"parent_thread_id":"{a.thread}"' in head_of(f)
                    or f'"session_id":"{a.thread}"' in head_of(f))]

    print(f'window {since:%H:%M}-{now:%H:%M} UTC')
    for f in lead_files + helpers:
        r = scan(f, since)
        if not r['last'] or r['last'] < since:
            continue
        if a.out:
            os.makedirs(a.out, exist_ok=True)
            with open(os.path.join(a.out, r['name'] + '.txt'), 'w', encoding='utf-8') as fh:
                fh.write(f"{r['name']} {r['path']}\n" + '\n'.join(r['timeline']) + '\n')
        idle = int((now - r['last']).total_seconds() // 60) if r['last'] else None
        total = sum(r['cmds'].values())
        print(f"\n[{r['name']}] idle {idle} min; commands {total}; failed {len(r['fails'])}; "
              f"compactions {r['compactions']}; collab calls {r['spawns']}")
        for c, n in r['cmds'].most_common(3):
            if n >= 3:
                print(f'  REPEAT x{n} (identical): {c[:160]}')
        for f_ in r['fails'][-3:]:
            print(f'  fail {f_[0]} exit {f_[1]}: {f_[2]}')
        for m in r['msgs'][-2:]:
            print(f'  says {m[0]}: {m[1]}')

    if not a.worktree:
        return
    out = subprocess.run(['git', '-C', a.worktree, 'status', '--short'],
                         capture_output=True, text=True).stdout.splitlines()
    recent = []
    for line in out:
        path = os.path.join(a.worktree, line[3:])
        for p in ([path] if os.path.isfile(path) else glob.glob(os.path.join(path, '**', '*'), recursive=True)):
            if os.path.isfile(p) and dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc) >= since:
                recent.append(os.path.relpath(p, a.worktree))
    print(f'\nworktree: {len(out)} changed paths; touched in window: {len(recent)}')
    for p in recent[:8]:
        print('  ' + p)


if __name__ == '__main__':
    main()
