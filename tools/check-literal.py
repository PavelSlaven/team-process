"""Flag vague wording, bare limits and a missing work-organisation section in a task or plan."""
import argparse
import re
import sys

VAGUE = [
    'по возможности', 'при необходимости', 'если нужно', 'если потребуется', 'и т. п.', 'и т.п.',
    'и т. д.', 'и т.д.', 'и подобное', 'и прочее', 'и др.', 'аналогично', 'как обычно', 'как сейчас',
    'разумно', 'разумный', 'где уместно', 'при желании', 'по желанию', 'желательно', 'по усмотрению',
    'по ситуации', 'лучше', 'оптимально', 'достаточно', 'как можно', 'etc',
]
VAGUE_RE = re.compile(
    r'(?<!\w)(?:' + '|'.join(re.escape(p).replace(r'\ ', r'\s+') for p in VAGUE) + r')(?!\w)',
    re.IGNORECASE)
LIMIT_RE = re.compile(
    r'(?<!\w)(?:не\s+больше|не\s+более|максимум|до)\s+\d+\s+'
    r'(?:помощник|агент|субагент|исполнител|поток|процесс)\w*', re.IGNORECASE)
LAUNCH_WORDS = ('запускай', 'запусти', 'сразу', 'одновременно все', 'целев')
SECTION_TITLE = 'Организация работы'
SECTION_ITEMS = ('Порядок', 'Параллельно', 'Помощники', 'Остановки', 'Рамка правок')
HEADING_RE = re.compile(r'^\s{0,3}(#+)\s')
BULLET_PREFIX_RE = re.compile(r'^[-*\s]*')


def checked_lines(text):
    """Source lines with code blocks, inline code and quotes blanked out."""
    result, in_block = [], False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            in_block = not in_block
            result.append('')
        elif in_block or line.lstrip().startswith('>'):
            result.append('')
        else:
            result.append(re.sub(r'`[^`]*`', ' ', line))
    return result


def check_wording(lines):
    for n, line in enumerate(lines, 1):
        for m in VAGUE_RE.finditer(line):
            yield n, 'РАСПЛЫВЧАТО', m.group(0), 'точный список, условие или число'
        if not any(w in line.lower() for w in LAUNCH_WORDS):
            for m in LIMIT_RE.finditer(line):
                yield (n, 'ПРЕДЕЛ', m.group(0),
                       'назван только предел — напиши, сколько запускать и когда: '
                       'независимое — сразу, до N одновременно')


def check_section(lines):
    headings = [(n, len(HEADING_RE.match(l).group(1)), l.strip()) for n, l in enumerate(lines, 1)
                if HEADING_RE.match(l)]
    start = next((h for h in headings if SECTION_TITLE.lower() in h[2].lower()), None)
    if not start:
        yield (1, 'РАЗДЕЛ', 'нет заголовка «Организация работы»',
               'нужен раздел «Организация работы»: порядок и зависимости, что параллельно, '
               'сколько помощников одновременно, остановки, рамка правок')
        return
    n0, level, _ = start
    end = next((n for n, lv, _ in headings if n > n0 and lv <= level), len(lines) + 1)
    body = [BULLET_PREFIX_RE.sub('', l).lower() for l in lines[n0:end - 1]]
    for item in SECTION_ITEMS:
        if not any(b.startswith(item.lower()) for b in body):
            yield (n0, 'РАЗДЕЛ', f'в разделе нет строки «{item}»',
                   f'добавь строку, начинающуюся со слова «{item}»')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--task', action='store_true', help='also require the work-organisation section')
    ap.add_argument('files', nargs='+')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')

    total = 0
    for path in a.files:
        with open(path, encoding='utf-8-sig') as fh:
            lines = checked_lines(fh.read())
        flags = list(check_wording(lines))
        if a.task:
            flags += check_section(lines)
        for n, kind, frag, hint in sorted(flags, key=lambda f: f[0]):
            print(f'{path}:{n}: {kind}: {frag} — {hint}')
        total += len(flags)
    print(f'флагов: {total}')
    sys.exit(1 if total else 0)


if __name__ == '__main__':
    main()
