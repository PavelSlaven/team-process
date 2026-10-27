# team-process

Навык Claude Code: рабочий процесс команды — владелец, ведущий-ревьюер, исполнители (Codex, Cursor, Claude), субагенты и независимые проверяющие. Карточка запуска задачи, план исполнителя до работы, сдача с доказательствами, проверки скриптами, независимое ревью, лимиты и выбор моделей.

## Состав

- `SKILL.md` — основной процесс; Claude Code загружает его, когда задача подходит под описание навыка.
- `fleet.md` — связь с исполнителями, флот, модели и лимиты.
- `checks.md` — детерминированные проверки сдачи.
- `bench.md` — стенды для решений, зависящих от поведения модели.
- `briefs.md` — каталог ловушек заданий.
- `executor-rules.md` — правила для исполнителей, которые включаются в задание.
- `lessons.md` — уроки.
- `tools/check-subagent-input.mjs` — проверка фактического входа субагента Workflow; тест: `node --test tools/check-subagent-input.test.mjs`.

Проектная специфика (пути, роли, журналы, номера решений) в навык не входит: она лежит в памяти проекта, а навык ссылается на неё словами «в памяти проекта».

## Установка

```bash
git clone https://github.com/PavelSlaven/team-process.git ~/.claude/skills/team-process
```

Навык становится доступен во всех проектах. Общие правила владельца, на которые он опирается, — в `~/.claude/CLAUDE.md`.

## Версия для Codex

`codex/` — тот же процесс для интерактивной сессии Codex, где Codex — ведущий-ревьюер: инструменты Codex (`spawn_agent`, `followup_task`, `wait_agent`) вместо механизмов Claude Code, общие правила владельца — в `codex/SKILL.md` §0 (Codex не читает `~/.claude/CLAUDE.md`).

- `codex/SKILL.md`, `codex/references/fleet.md`, `codex/references/lessons.md` — переложены под Codex.
- `codex/references/{executor-rules,briefs,checks,bench}.md` — побайтовые копии корневых файлов.

Установка — копией (папка Codex не git-клон). Резервная копия — в `~/.codex/`, а не в `~/.codex/skills/`: папка с `SKILL.md` внутри `skills/` стала бы вторым навыком `team-process`.

```bash
mv ~/.codex/skills/team-process ~/.codex/team-process.bak-$(date +%Y%m%d) && cp -r ~/.claude/skills/team-process/codex ~/.codex/skills/team-process
```

## Обновление

Правка навыка — сразу коммит и push в этом репозитории. Правило меняется в обеих версиях одним коммитом; копии сверяет команда (пустой вывод — совпадают):

```bash
cd ~/.claude/skills/team-process && for f in executor-rules briefs checks bench; do cmp -s $f.md codex/references/$f.md || echo "differs: $f.md"; done
```

После правки `codex/` — повторить установку. На другой машине — `git -C ~/.claude/skills/team-process pull`. Резервные копии `*.bak-*` в репозиторий не попадают.
