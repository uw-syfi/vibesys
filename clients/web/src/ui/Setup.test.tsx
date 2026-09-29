import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AuthStatus, Catalog, TaskDetail, TaskSummary} from '../home-api.js';
import {initialForm, NEW_TASK, withTask} from '../setup.js';
import {Advanced, FolderRow, ModelRow, Roles, SetupFooter, TaskRow} from './Setup.js';

const load = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../fixtures/${name}`, import.meta.url), 'utf8'));
const CATALOG = load('home-catalog.json') as Catalog;
const AUTH = load('home-auth.json') as AuthStatus;
const FORM = {...initialForm(CATALOG, AUTH, '/r'), task: 'decode'};
const TASKS: TaskSummary[] = [
  {name: 'decode', valid: true, domain: 'llm-serving', error: null},
  {name: 'broken', valid: false, domain: null, error: 'bad key'},
];
const DECODE: TaskDetail = {
  name: 'decode',
  objective: 'Increase decode throughput without changing outputs.',
  domain: 'llm-serving',
  accuracy_command: 'cargo test --release',
  benchmark_command: 'cargo bench --bench decode',
  result: {
    kind: 'metric',
    json_argument: '--json',
    metric: 'median_tok_per_sec',
    protocol_version: null,
  },
  profile_guided: false,
  editable: true,
  read_only_reason: null,
  content_hash: 'h1',
};
const none = () => undefined;

test('the folder row: its status, and Browse and Commit only where the view offers them', () => {
  const status = {tone: 'bad' as const, text: 'Task files are not committed.', commit: true};
  const bare = renderToStaticMarkup(
    <FolderRow path="/r" recent={['/r']} status={status} onPath={none} onCheck={none} />,
  );
  assert.match(bare, /<input id="f-folder" class="fld mono bad" list="recent-folders"/);
  assert.match(bare, /<option value="\/r">/);
  assert.match(bare, /<div class="hint bad" role="alert">Task files are not committed\./);
  assert.doesNotMatch(bare, /Commit task files|Browse/);
  const full = renderToStaticMarkup(
    <FolderRow
      path="/r"
      recent={[]}
      status={status}
      onPath={none}
      onCheck={none}
      onBrowse={none}
      onCommit={none}
    />,
  );
  assert.match(full, />Browse…<\/button>/);
  assert.match(full, />Commit task files…<\/button>/);
});

test('a saved task is a card whose title picks the task; read-only tasks say why on hover', () => {
  const html = renderToStaticMarkup(
    <TaskRow
      form={FORM}
      tasks={TASKS}
      detail={DECODE}
      disabled={false}
      onTask={none}
      onEdit={none}
    />,
  );
  assert.match(html, /<div class="summary"><span class="sel"><select id="f-task" class="t">/);
  assert.match(
    html,
    /<option value="broken" disabled="" title="bad key">broken \(invalid\)<\/option>/,
  );
  assert.match(html, /<option value="\+new">New task…<\/option>/);
  assert.match(
    html,
    />cargo bench --bench decode<\/span>, <span class="nw">median_tok_per_sec, higher is better<\/span>/,
  );
  assert.match(html, />Edit<\/button>/);
  const readOnly = renderToStaticMarkup(
    <TaskRow
      form={FORM}
      tasks={TASKS}
      detail={{
        ...DECODE,
        editable: false,
        read_only_reason: 'The benchmark is an evaluator entrypoint',
      }}
      disabled={false}
      onTask={none}
    />,
  );
  assert.match(
    readOnly,
    /<span class="ro" title="The benchmark is an evaluator entrypoint">Read-only<\/span>/,
  );
  const creating = renderToStaticMarkup(
    <TaskRow
      form={withTask(FORM, NEW_TASK)}
      tasks={TASKS}
      detail={null}
      disabled={false}
      onTask={none}
    />,
  );
  assert.match(creating, /<select id="f-task" class="fld">/);
  const noFolder = renderToStaticMarkup(
    <TaskRow form={{...FORM, task: null}} tasks={[]} detail={null} disabled onTask={none} />,
  );
  assert.match(
    noFolder,
    /disabled=""><option value="" selected="">Choose a folder first<\/option>/,
  );
});

test('per-role models, and effort only where the provider supports it', () => {
  const roles = renderToStaticMarkup(
    <Roles roles={['orchestrator', 'implementer', 'judge']} form={FORM} effort onRole={none} />,
  );
  assert.match(roles, /<summary class="disc">.*Use a different model per role<\/summary>/);
  assert.match(roles, /aria-label="Judge model" placeholder="claude-opus-5"/);
  assert.match(roles, /aria-label="Judge reasoning effort"/);
  const advanced = renderToStaticMarkup(
    <Advanced
      catalog={CATALOG}
      form={{...FORM, provider: 'gemini'}}
      effort={false}
      onLoop={none}
      onChange={none}
    />,
  );
  assert.match(advanced, /<option value="metal" selected="">Metal<\/option>/);
  assert.match(advanced, /<option value="profile-guided">Profile-guided<\/option>/);
  assert.doesNotMatch(advanced, /f-effort/);
  assert.doesNotMatch(advanced, /Omnigent/);
});

test('the footer lists blockers as links; busy or blocked disables Start', () => {
  const blocked = renderToStaticMarkup(
    <SetupFooter
      blockers={[
        {field: 'folder', text: 'Folder has uncommitted changes'},
        {field: 'key', text: 'Codex CLI key is needed'},
      ]}
      busy={null}
      error={null}
      cancelHref="/?token=h"
      onFix={none}
      onStart={none}
    />,
  );
  assert.match(
    blocked,
    /<span>2 to fix:<\/span><button type="button" class="linkbtn">Folder has uncommitted changes<\/button>/,
  );
  assert.match(blocked, /<a class="btn ghost" href="\/\?token=h">Cancel<\/a>/);
  assert.match(
    blocked,
    /<button type="button" class="btn primary" disabled="">Start run<\/button>/,
  );
  const busy = renderToStaticMarkup(
    <SetupFooter
      blockers={[]}
      busy="Starting the run…"
      error={null}
      cancelHref="/"
      onFix={none}
      onStart={none}
    />,
  );
  assert.match(busy, /<span class="spin"><\/span>Starting the run…/);
  assert.match(busy, /disabled="">Start run/);
  const ready = renderToStaticMarkup(
    <SetupFooter
      blockers={[]}
      busy={null}
      error={null}
      cancelHref="/"
      onFix={none}
      onStart={none}
    />,
  );
  assert.match(ready, /<button type="button" class="btn primary">Start run<\/button>/);
});

test('provider and model read as one field', () => {
  const html = renderToStaticMarkup(
    <ModelRow catalog={CATALOG} form={FORM} onProvider={none} onModel={none} />,
  );
  assert.match(html, /<div class="fld model"><select aria-label="Provider">/);
  assert.match(html, /<input id="f-model" list="models" placeholder="Model name"/);
  assert.equal(html.match(/<svg/g)?.length, 1);
});
