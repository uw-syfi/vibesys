import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {
  AuthStatus,
  Catalog,
  ProjectValidation,
  ProviderAuth,
  TaskDetail,
  TaskSummary,
} from './home-api.js';
import {HomeError} from './home-api.js';
import {
  blockers,
  draftOf,
  folderStatus,
  initialForm,
  keyView,
  NEW_TASK,
  newDraft,
  type Readiness,
  resultText,
  roleSummary,
  saveError,
  startRequest,
  withLoop,
  withoutDraft,
  withProvider,
  withTask,
  withTasks,
} from './setup.js';

const load = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8'));
const CATALOG = load('home-catalog.json') as Catalog;
const AUTH = load('home-auth.json') as AuthStatus;
const provider = (name: string): ProviderAuth => {
  const row = AUTH.providers.find(item => item.provider === name);
  if (row === undefined) throw new Error(name);
  return row;
};
const ROOT = '/Users/me/src/llm-serve';
const READY: ProjectValidation = {
  state: 'ready',
  path: ROOT,
  project: {id: 'p1', root: ROOT, name: 'llm-serve'},
  message: null,
  tasks: ['decode'],
  pending: [],
};
const TASKS: TaskSummary[] = [
  {name: 'broken', valid: false, domain: null, error: 'vibesys.input.toml: bad key'},
  {name: 'decode', valid: true, domain: 'llm-serving', error: null},
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
const ready = (patch: Partial<Readiness> = {}): Readiness => ({
  form: withTasks(initialForm(CATALOG, AUTH, ROOT), TASKS),
  validation: READY,
  tasks: TASKS,
  detail: DECODE,
  catalog: CATALOG,
  auth: AUTH,
  ...patch,
});

test('defaults: the first provider with a key, its first model, the first loop and the host backend', () => {
  const form = initialForm(CATALOG, AUTH, ROOT);
  assert.deepEqual(
    {...form},
    {
      path: ROOT,
      task: null,
      draft: null,
      budget: '12',
      loop: 'agent',
      compute: 'metal',
      driver: null,
      provider: 'claude',
      model: 'claude-opus-5',
      effort: '',
      roles: {},
    },
  );
  const none = {
    ...AUTH,
    providers: AUTH.providers.map(row => ({...row, status: 'missing' as const})),
  };
  assert.equal(initialForm(CATALOG, none, '').provider, 'claude');
});

test('a provider change resets its model, effort, roles and an unsupported driver; a loop change its budget', () => {
  const form = {
    ...initialForm(CATALOG, AUTH, ROOT),
    driver: 'omnigent' as const,
    effort: 'high',
    roles: {judge: {model: 'claude-haiku-4-5', effort: ''}},
  };
  const gemini = withProvider(form, CATALOG, 'gemini');
  assert.deepEqual([gemini.model, gemini.effort, gemini.roles, gemini.driver], ['', '', {}, null]);
  assert.equal(withProvider(form, CATALOG, 'codex').driver, 'omnigent');
  assert.deepEqual(
    [withLoop(form, CATALOG, 'evolve').loop, withLoop(form, CATALOG, 'evolve').budget],
    ['evolve', '8'],
  );
  assert.equal(withLoop(form, CATALOG, 'nope'), form);
});

test('tasks: a valid choice stays, else the first valid one, else a new task form', () => {
  const form = initialForm(CATALOG, AUTH, ROOT);
  assert.equal(withTasks(form, TASKS).task, 'decode');
  assert.equal(withTasks({...form, task: 'broken'}, TASKS).task, 'decode');
  const fresh = withTasks(form, []);
  assert.deepEqual([fresh.task, fresh.draft], [NEW_TASK, newDraft()]);
  const creating = withTask(form, NEW_TASK);
  assert.equal(withTasks(creating, TASKS), creating);
  assert.deepEqual(withTask(creating, 'decode').draft, null);
});

test('a ready folder, a saved task and a present key block nothing', () => {
  assert.deepEqual(blockers(ready()), []);
});

test('each blocker names its field, in form order', () => {
  const dirty: ProjectValidation = {...READY, state: 'dirty_tree', pending: ['src/batch.rs']};
  const form = {...withProvider(ready().form, CATALOG, 'codex'), budget: '0', model: ' '};
  assert.deepEqual(blockers(ready({validation: dirty, form})), [
    {field: 'folder', text: 'Folder has uncommitted changes'},
    {field: 'budget', text: 'Rounds must be a whole number'},
    {field: 'model', text: 'Model is empty'},
    {field: 'key', text: 'Codex CLI key is needed'},
  ]);
  const cliOnly = withProvider(ready().form, CATALOG, 'opencode');
  assert.deepEqual(blockers(ready({form: cliOnly})), [
    {field: 'key', text: 'Sign in to OpenCode from a terminal'},
  ]);
  assert.deepEqual(blockers(ready({form: {...ready().form, path: ''}, validation: null})), [
    {field: 'folder', text: 'Choose a folder'},
  ]);
});

test('uncommitted task files, a new task and profile-guided prerequisites are blockers', () => {
  const taskFiles: ProjectValidation = {
    ...READY,
    state: 'dirty_tree',
    pending: ['.vibesys/tasks/decode/OBJECTIVE.md'],
  };
  assert.deepEqual(blockers(ready({validation: taskFiles})), [
    {field: 'folder', text: 'Task files are not committed'},
  ]);
  const creating = withTask(ready().form, NEW_TASK);
  assert.deepEqual(
    blockers(ready({form: creating, detail: null})).map(item => item.field),
    ['name', 'objective', 'accuracy', 'benchmark', 'metric'],
  );
  const complete = {
    ...creating,
    draft: {...draftOf(DECODE), name: 'decode-2'},
  };
  assert.deepEqual(blockers(ready({form: complete, detail: null})), [
    {field: 'save', text: 'Save the task'},
  ]);
  const guided = withLoop(ready().form, CATALOG, 'profile-guided');
  assert.deepEqual(blockers(ready({form: guided})), [
    {field: 'loop', text: 'Profile-guided needs a task with a [profile_guided] section'},
  ]);
  assert.deepEqual(blockers(ready({form: guided, detail: {...DECODE, profile_guided: true}})), []);
});

test('folder status: next actions per state, and commit only when task files are pending', () => {
  assert.deepEqual(folderStatus(READY, false, null), {
    tone: 'ok',
    text: 'Git repository, working tree clean',
    commit: false,
  });
  assert.deepEqual(folderStatus(null, true, null), {
    tone: 'plain',
    text: 'Checking…',
    commit: false,
  });
  assert.deepEqual(folderStatus(null, false, 'Path is outside the granted roots'), {
    tone: 'bad',
    text: 'Path is outside the granted roots',
    commit: false,
  });
  assert.equal(folderStatus(null, false, null), null);
  assert.deepEqual(
    folderStatus(
      {
        ...READY,
        state: 'dirty_tree',
        pending: ['.vibesys/tasks/a/OBJECTIVE.md', 'a.rs', 'b.rs', 'c.rs'],
      },
      false,
      null,
    ),
    {
      tone: 'bad',
      text: 'Uncommitted changes in a.rs, b.rs and 1 more. Commit or stash them so rounds can revert cleanly.',
      commit: true,
    },
  );
  assert.equal(folderStatus({...READY, state: 'no_commits'}, false, null)?.commit, true);
  assert.equal(
    folderStatus({...READY, state: 'uninitialized'}, false, null)?.text,
    'No VibeSys tasks here yet. Create one below.',
  );
});

test('key rows: present, missing, CLI-only, saving, saved, rejected, shadowed', () => {
  const where = '/Users/me/vibesys/.env';
  const claude = keyView(provider('claude'), {kind: 'idle'}, where);
  assert.deepEqual(
    [claude.label, claude.name, claude.tone, claude.hint, claude.where],
    [
      // Labeled by the vendor the key belongs to, not the CLI it authenticates (Claude Code -> Anthropic).
      'Anthropic API key',
      'ANTHROPIC_API_KEY',
      'ok',
      'Saved in .env. Unverified until the first run.',
      `Written to ANTHROPIC_API_KEY in ${where}`,
    ],
  );
  const codex = keyView(provider('codex'), {kind: 'idle'}, where);
  assert.deepEqual(
    [codex.label, codex.placeholder, codex.tone, codex.login],
    ['OpenAI API key', 'Paste a key…', 'plain', null],
  );
  const gemini = keyView(provider('gemini'), {kind: 'idle'}, where);
  assert.equal(gemini.label, 'Gemini API key');
  const opencode = keyView(provider('opencode'), {kind: 'idle'}, where);
  // No key variable to derive a vendor from: falls back to the provider's own display name.
  assert.deepEqual(
    [opencode.label, opencode.name, opencode.login],
    ['OpenCode key', null, 'opencode auth login'],
  );
  assert.equal(keyView(provider('codex'), {kind: 'saving'}, where).hint, 'Saving…');
  assert.deepEqual(
    [
      keyView(provider('codex'), {kind: 'saved', shadowed: false}, where).tone,
      keyView(provider('codex'), {kind: 'saved', shadowed: true}, where).tone,
    ],
    ['ok', 'warn'],
  );
  const rejected = keyView(
    provider('codex'),
    {kind: 'rejected', message: 'The key contains a quote'},
    where,
  );
  assert.deepEqual([rejected.tone, rejected.hint], ['bad', 'Rejected: The key contains a quote']);
  const shadowed = keyView(
    {...provider('claude'), keys: [{name: 'ANTHROPIC_API_KEY', source: 'env', shadowed: true}]},
    {kind: 'idle'},
    where,
  );
  assert.deepEqual(
    [shadowed.tone, shadowed.hint, shadowed.placeholder],
    [
      'warn',
      "ANTHROPIC_API_KEY in this app's environment overrides .env.",
      // Must not invite pasting a key that the environment variable would still override.
      'An environment variable is in use…',
    ],
  );
});

test('the start request trims, keeps only the chosen loop roles that were set, and nulls blanks', () => {
  const form = {
    ...ready().form,
    budget: ' 20 ',
    model: ' claude-opus-5 ',
    roles: {
      judge: {model: 'claude-haiku-4-5', effort: ''},
      implementer: {model: ' ', effort: ' '},
      mutator: {model: 'x', effort: ''},
    },
  };
  assert.deepEqual(startRequest(form, CATALOG), {
    task: 'decode',
    outer_loop: 'agent',
    budget: 20,
    compute_backend: 'metal',
    driver: null,
    provider: 'claude',
    model: 'claude-opus-5',
    reasoning_effort: null,
    roles: {judge: {model: 'claude-haiku-4-5', reasoning_effort: null}},
  });
});

test('summaries: the per-role disclosure counts overrides; results say how they are read', () => {
  const form = ready().form;
  const roles = ['orchestrator', 'implementer', 'judge'];
  assert.equal(roleSummary(form, roles), 'Use a different model per role');
  assert.equal(
    roleSummary({...form, roles: {judge: {model: 'm', effort: ''}}}, roles),
    '1 role has its own model',
  );
  assert.equal(resultText(DECODE), 'median_tok_per_sec, higher is better');
  assert.equal(resultText({...DECODE, editable: false}), 'median_tok_per_sec');
  assert.equal(
    resultText({...DECODE, result: {...DECODE.result, kind: 'protocol'}}),
    'reported through the result protocol',
  );
});

test('Discard returns an edit to the saved task and a new task to the first valid one', () => {
  const editing = {...ready().form, draft: draftOf(DECODE)};
  assert.deepEqual(
    [withoutDraft(editing, TASKS).task, withoutDraft(editing, TASKS).draft],
    ['decode', null],
  );
  const creating = withTask(ready().form, NEW_TASK);
  assert.deepEqual(
    [withoutDraft(creating, TASKS).task, withoutDraft(creating, TASKS).draft],
    ['decode', null],
  );
  const only = withoutDraft(creating, []);
  assert.equal(only.task, NEW_TASK);
  assert.equal(only.draft?.objective, '');
});

test('a refused save reads as one line that says what to do', () => {
  const refused = (code: 'task_conflict' | 'task_exists' | 'task_read_only', message: string) =>
    saveError(new HomeError(code, message, null));
  assert.equal(
    refused('task_conflict', 'the task changed on disk since it was loaded; reload it'),
    'The task changed on disk. Discard to load it, then edit again.',
  );
  assert.equal(
    refused('task_exists', 'task decode already exists'),
    'A task with this name already exists.',
  );
  assert.equal(
    refused('task_read_only', 'the manifest uses [[metrics]]'),
    'Read-only: the manifest uses [[metrics]]',
  );
  assert.equal(
    saveError(
      new HomeError('task_invalid', 'the task manifest is invalid', {
        errors: [{msg: 'bad metric'}],
      }),
    ),
    'the task manifest is invalid\nbad metric',
  );
});
