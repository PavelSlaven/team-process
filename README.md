# team-process

Навык Claude Code: рабочий процесс команды — владелец, ведущий-ревьюер, исполнители (Codex, Cursor, Claude), субагенты и независимые проверяющие. Карточка запуска задачи, план исполнителя до работы, сдача с доказательствами, проверки скриптами, независимое ревью, лимиты и выбор моделей.

## Состав

- `SKILL.md` — основной процесс; Claude Code загружает его, когда задача подходит под описание навыка.
- `fleet.md` — связь с исполнителями, флот, модели и лимиты.
- `checks.md` — детерминированные проверки сдачи.
- `bench.md` — стенды для решений, зависящих от поведения модели.
- `briefs.md` — каталог ловушек заданий.
- `executor-rules.md` — правила для исполнителей, которые включаются в задание.
- `cleanup.md` — уборка после задачи: папка задачи, раздел «Рабочие места» в сдаче, опись и удаление при закрытии.
- `lessons.md` — уроки.
- `tools/check-subagent-input.mjs` — проверка фактического входа субагента Workflow; тест: `node --test tools/check-subagent-input.test.mjs`.
- `tools/check-reads.py [--deleg-ok] <журнал.jsonl> <путь>...` — проверка полного чтения файлов по журналу Claude или Codex (rollout; формат определяется по каждой записи). Статусы: `READ` (Read без offset и limit или `cat <путь>` с полным выводом), `DELEG` (путь в `prompt` вызова `Agent` или `Task` в журнале Claude), `PART` (любое другое упоминание пути в команде), `MISS`. Относительные пути разрешаются от рабочей папки вызова: `workdir` вызова Codex, иначе последняя `cwd` из `turn_context`/`session_meta` (Codex) или `cwd` записи (Claude); `cd <папка>` в команде меняет папку для следующих частей. Вывод Codex с `Warning: truncated output` даёт `PART` для файлов этого вызова. Чтение частями `sed -n 'a,bp'` (несколько диапазонов через `;` тоже; конец `$` — последняя строка) даёт `READ`, если объединение диапазонов необрезанных вызовов покрывает строки 1..N, где N — число строк файла на машине запуска скрипта; файла нет — покрытие не засчитывается. Учитываются только записи после последней границы сжатия контекста (`compact_boundary` в журнале Claude, `compacted` в журнале Codex). Код выхода 0, если каждый путь `READ`; с `--deleg-ok` годится и `DELEG`. Тест: `python3 -B -I -m unittest discover -s tools -p test_check_reads.py`.
- `tools/usage-daily.py` — дневной расход Claude и вызовы сессии с субагентами; тест: `python3 -B -I -m unittest discover -s tools -p test_usage_daily.py`.
- `tools/codex-watch.py` — хроника работы Codex и его помощников за окно времени (повторы, провалы, сжатия, рабочая копия); тест: `python -I -m unittest discover -s tools -p test_supervise_tools.py`.
- `python3 -I tools/codex-load-check.py --config <файл.json>` — проверяет загрузку Codex, активность нитей и наличие готовых задач, а нарушения выводит в отчёт и настроенный файл сигналов.
- `tools/check-literal.py` — проверка буквальности задания или плана (расплывчатые обороты, голые пределы, раздел «Организация работы» при `--task`); тест тот же.
- `tools/task-places.py` — опись мест задачи (рабочие копии, клоны, папки: несохранённые правки, коммиты не на удалённом, процессы, ссылки из живых файлов) и удаление с архивом несохранённого; тест: `python -I -m unittest discover -s tools -p test_task_places.py`.

Проектная специфика (пути, роли, журналы, номера решений) в навык не входит: она лежит в памяти проекта, а навык ссылается на неё словами «в памяти проекта».

## Установка

```bash
git clone https://github.com/PavelSlaven/team-process.git ~/.claude/skills/team-process
```

Навык становится доступен во всех проектах. Общие правила владельца, на которые он опирается, — в `~/.claude/CLAUDE.md`.

## Версия для Codex

`codex/` — тот же процесс для интерактивной сессии Codex, где Codex — ведущий-ревьюер: инструменты Codex (`spawn_agent`, `followup_task`, `wait_agent`) вместо механизмов Claude Code, общие правила владельца — в `codex/SKILL.md` §0 (Codex не читает `~/.claude/CLAUDE.md`).

- `codex/SKILL.md`, `codex/references/fleet.md`, `codex/references/lessons.md` — переложены под Codex.
- `codex/references/{executor-rules,briefs,checks,bench,cleanup}.md` — побайтовые копии корневых файлов.

Установка — копией (папка Codex не git-клон). Резервная копия — в `~/.codex/`, а не в `~/.codex/skills/`: папка с `SKILL.md` внутри `skills/` стала бы вторым навыком `team-process`.

```bash
mv ~/.codex/skills/team-process ~/.codex/team-process.bak-$(date +%Y%m%d) && cp -r ~/.claude/skills/team-process/codex ~/.codex/skills/team-process
```

## Обновление

Правка навыка — сразу коммит и push в этом репозитории. Правило меняется в обеих версиях одним коммитом; копии сверяет команда (пустой вывод — совпадают):

```bash
cd ~/.claude/skills/team-process && for f in executor-rules briefs checks bench cleanup; do cmp -s $f.md codex/references/$f.md || echo "differs: $f.md"; done
```

После правки `codex/` — повторить установку. На другой машине — `git -C ~/.claude/skills/team-process pull`. Резервные копии `*.bak-*` в репозиторий не попадают.
