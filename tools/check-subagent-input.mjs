#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { pathToFileURL } from 'node:url';

const RELAY_HEADER = '[Workflow harness — user request]';
const COMPUTED_HEADER = '[Workflow harness — computed task]';

function text(content) {
  if (typeof content === 'string') return content;
  return (content || []).filter(p => p.type === 'text').map(p => p.text).join('\n');
}

function readJsonl(file) {
  return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).flatMap(l => {
    try { return [JSON.parse(l)]; } catch { return []; }
  });
}

export function relayedRequest(first) {
  if (!first.startsWith(RELAY_HEADER)) return null;
  const m = first.match(/this request wins:\n([\s\S]*)$/);
  return m ? m[1].replace(/^ {2}/gm, '').trim() : '';
}

export async function agentPrompts(mainTranscript) {
  const prompts = new Map();
  const rl = readline.createInterface({ input: fs.createReadStream(mainTranscript, 'utf8'), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.includes('"tool_use"') || !line.includes('"Agent"')) continue;
    let o; try { o = JSON.parse(line); } catch { continue; }
    for (const p of o.message?.content || []) {
      if (p.type === 'tool_use' && p.name === 'Agent') prompts.set(p.id, p.input?.prompt ?? '');
    }
  }
  return prompts;
}

export function checkAgent(first, prompt) {
  if (first.includes(RELAY_HEADER) || first.includes(COMPUTED_HEADER)) return ['FAIL', 'во входе Agent есть обёртка Workflow'];
  if (prompt === undefined) return ['UNKNOWN', 'вызов Agent не найден в транскрипте сессии'];
  const a = first.replace(/\r\n/g, '\n'), b = prompt.replace(/\r\n/g, '\n');
  if (a === b) return ['OK', 'вход = промпт'];
  let i = 0; while (i < a.length && a[i] === b[i]) i++;
  return ['FAIL', `вход не совпадает с промптом с символа ${i}: «${a.slice(i, i + 80).replace(/\n/g, ' ')}»`];
}

export function checkWorkflowAgent(first, wfId, approved) {
  const relay = relayedRequest(first);
  if (relay === null) return ['OK', 'релея нет'];
  const shown = relay.slice(0, 120).replace(/\s+/g, ' ');
  if (/^<system-reminder>|<task-notification>|<side-sessions-event>/.test(relay)) return ['FAIL', `релей служебного сообщения как голоса пользователя: «${shown}»`];
  if (approved.has(wfId)) return ['OK', `релей одобрен как запрос этой работы: «${shown}»`];
  return ['FAIL', `релей сообщения владельца, не одобренный как запрос этой работы: «${shown}»`];
}

async function main(argv) {
  const args = { approve: new Set(), since: '', out: '' };
  const pos = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--approve-relay') argv[++i].split(',').filter(Boolean).forEach(x => args.approve.add(x));
    else if (argv[i] === '--since') args.since = argv[++i];
    else if (argv[i] === '--out') args.out = argv[++i];
    else pos.push(argv[i]);
  }
  if (pos.length !== 1) {
    console.error('usage: check-subagent-input.mjs <session-dir> [--since ISO] [--approve-relay wf_id,...] [--out file]');
    process.exit(2);
  }
  const sessionDir = pos[0].replace(/[\/]+$/, '');
  const sub = path.join(sessionDir, 'subagents');
  const prompts = await agentPrompts(sessionDir + '.jsonl');
  const rows = [];
  const firstUser = file => {
    const u = readJsonl(file).find(o => o.type === 'user');
    return u ? { text: text(u.message.content), ts: u.timestamp || '' } : null;
  };
  if (fs.existsSync(sub)) {
    for (const f of fs.readdirSync(sub).filter(f => /^agent-.*\.jsonl$/.test(f))) {
      const first = firstUser(path.join(sub, f)); if (!first || first.ts < args.since) continue;
      const metaFile = path.join(sub, f.replace(/\.jsonl$/, '.meta.json'));
      const meta = fs.existsSync(metaFile) ? JSON.parse(fs.readFileSync(metaFile, 'utf8')) : {};
      const [st, why] = checkAgent(first.text, prompts.get(meta.toolUseId));
      rows.push([st, first.ts, 'agent', f.slice(6, -6), why]);
    }
    const wfRoot = path.join(sub, 'workflows');
    if (fs.existsSync(wfRoot)) {
      for (const wf of fs.readdirSync(wfRoot)) {
        for (const f of fs.readdirSync(path.join(wfRoot, wf)).filter(f => /^agent-.*\.jsonl$/.test(f))) {
          const first = firstUser(path.join(wfRoot, wf, f)); if (!first || first.ts < args.since) continue;
          const [st, why] = checkWorkflowAgent(first.text, wf, args.approve);
          rows.push([st, first.ts, 'workflow', `${wf}/${f.slice(6, -6)}`, why]);
        }
      }
    }
  }
  rows.sort((a, b) => a[1].localeCompare(b[1]));
  const fails = rows.filter(r => r[0] === 'FAIL').length, unknown = rows.filter(r => r[0] === 'UNKNOWN').length;
  const out = rows.map(r => r.join('\t')).join('\n') + `\nИТОГ ${fails ? 'FAIL' : unknown ? 'UNKNOWN' : 'OK'}: агентов ${rows.length}, FAIL ${fails}, UNKNOWN ${unknown}\n`;
  if (args.out) fs.writeFileSync(args.out, out);
  process.stdout.write(args.out ? out.split('\n').slice(-2).join('\n') : out);
  process.exit(fails ? 1 : unknown ? 3 : 0);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main(process.argv.slice(2));
