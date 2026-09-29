/**
 * The New run form as data: defaults from the catalog, what blocks Start (each blocker names the
 * field that fixes it), the folder and key status lines, and the start request. Pure; `SetupView`
 * owns the state and the API calls.
 */
import type {
  AuthStatus,
  Catalog,
  ComputeBackend,
  Driver,
  OuterLoop,
  OuterLoopId,
  ProjectValidation,
  ProviderAuth,
  StartRun,
  TaskDetail,
  TaskDomain,
  TaskForm,
  TaskSummary,
} from './home-api.js';

/** The task select's "New task" value; task names start with [a-z0-9], so it cannot collide. */
export const NEW_TASK = '+new';
const TASKS_ROOT = '.vibesys/tasks/';
const TASK_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const WHOLE = /^[1-9]\d*$/;

export type FieldId =
  | 'folder'
  | 'task'
  | 'name'
  | 'objective'
  | 'accuracy'
  | 'benchmark'
  | 'metric'
  | 'save'
  | 'budget'
  | 'loop'
  | 'model'
  | 'key';

/** The DOM id of the control that fixes a blocker. */
export const fieldId = (field: FieldId): string => `f-${field}`;

export interface Draft {
  name: string;
  objective: string;
  domain: TaskDomain;
  accuracy_command: string;
  benchmark_command: string;
  result_json_argument: string;
  result_metric: string;
}

export interface RoleChoice {
  model: string;
  effort: string;
}

export interface SetupForm {
  path: string;
  /** A task name, `NEW_TASK`, or null until the folder's tasks are known. */
  task: string | null;
  /** The task form while creating (`task === NEW_TASK`) or editing; null shows the saved task. */
  draft: Draft | null;
  budget: string;
  loop: OuterLoopId;
  compute: ComputeBackend;
  driver: Driver | null;
  provider: string;
  model: string;
  effort: string;
  roles: Readonly<Record<string, RoleChoice>>;
}

export interface Blocker {
  field: FieldId;
  text: string;
}

export function suggestedModels(catalog: Catalog, provider: string): readonly string[] {
  return catalog.providers.find(option => option.provider === provider)?.suggested_models ?? [];
}

function defaultProvider(catalog: Catalog, auth: AuthStatus): string {
  const signedIn = catalog.providers.find(option =>
    auth.providers.some(row => row.provider === option.provider && row.status !== 'missing'),
  );
  return (signedIn ?? catalog.providers[0])?.provider ?? '';
}

export function initialForm(catalog: Catalog, auth: AuthStatus, path: string): SetupForm {
  const loop = catalog.outer_loops[0];
  const provider = defaultProvider(catalog, auth);
  return {
    path,
    task: null,
    draft: null,
    budget: loop === undefined ? '' : String(loop.budget.default),
    loop: loop?.id ?? 'agent',
    compute: catalog.default_compute_backend,
    driver: null,
    provider,
    model: suggestedModels(catalog, provider)[0] ?? '',
    effort: '',
    roles: {},
  };
}

/** Role models of another provider would not run, so they go with the provider. */
export function withProvider(form: SetupForm, catalog: Catalog, provider: string): SetupForm {
  const driver = catalog.drivers.some(
    option => option.driver === form.driver && option.providers.includes(provider),
  )
    ? form.driver
    : null;
  return {
    ...form,
    provider,
    model: suggestedModels(catalog, provider)[0] ?? '',
    effort: '',
    roles: {},
    driver,
  };
}

export function withLoop(form: SetupForm, catalog: Catalog, loopId: string): SetupForm {
  const loop = catalog.outer_loops.find(option => option.id === loopId);
  return loop === undefined ? form : {...form, loop: loop.id, budget: String(loop.budget.default)};
}

export function newDraft(): Draft {
  return {
    name: '',
    objective: '',
    domain: 'generic',
    accuracy_command: '',
    benchmark_command: '',
    result_json_argument: '--json',
    result_metric: '',
  };
}

export function withTask(form: SetupForm, name: string): SetupForm {
  return name === NEW_TASK
    ? {...form, task: NEW_TASK, draft: newDraft()}
    : {...form, task: name, draft: null};
}

/** Keeps a valid choice; otherwise the first valid task, or a new task form when there is none. */
export function withTasks(form: SetupForm, tasks: readonly TaskSummary[]): SetupForm {
  const kept = tasks.some(task => task.valid && task.name === form.task);
  if (form.task === NEW_TASK || kept) return form;
  const first = tasks.find(task => task.valid)?.name;
  return withTask(form, first ?? NEW_TASK);
}

export function draftOf(detail: TaskDetail): Draft {
  return {
    name: detail.name,
    objective: detail.objective,
    domain: detail.domain,
    accuracy_command: detail.accuracy_command,
    benchmark_command: detail.benchmark_command,
    result_json_argument: detail.result.json_argument ?? '--json',
    result_metric: detail.result.metric ?? '',
  };
}

export function taskForm(draft: Draft): TaskForm {
  return {
    objective: draft.objective,
    domain: draft.domain,
    accuracy_command: draft.accuracy_command,
    benchmark_command: draft.benchmark_command,
    result_json_argument: draft.result_json_argument,
    result_metric: draft.result_metric,
  };
}

export const isTaskFile = (path: string): boolean => path.startsWith(TASKS_ROOT);

export function budgetLabel(loop: OuterLoop | undefined): 'Rounds' | 'Generations' {
  return loop?.budget.flag === '--max-generations' ? 'Generations' : 'Rounds';
}

export interface Readiness {
  form: SetupForm;
  validation: ProjectValidation | null;
  tasks: readonly TaskSummary[];
  detail: TaskDetail | null;
  catalog: Catalog;
  auth: AuthStatus;
}

function folderBlocker(validation: ProjectValidation): string | null {
  switch (validation.state) {
    case 'ready':
    case 'uninitialized':
    case 'no_tasks':
      return null;
    case 'missing':
      return 'Folder does not exist';
    case 'not_git':
      return 'Folder is not a git repository';
    case 'invalid':
      return 'Folder layout is invalid';
    case 'no_commits':
      return 'Folder has no commits';
    case 'dirty_tree':
      return validation.pending.every(isTaskFile)
        ? 'Task files are not committed'
        : 'Folder has uncommitted changes';
  }
}

function folderBlockers(path: string, validation: ProjectValidation | null): Blocker[] {
  if (path.trim() === '') return [{field: 'folder', text: 'Choose a folder'}];
  if (validation === null) return [{field: 'folder', text: 'Folder is not checked yet'}];
  const text = folderBlocker(validation);
  return text === null ? [] : [{field: 'folder', text}];
}

export function draftBlockers(draft: Draft, creating: boolean): Blocker[] {
  const missing: Blocker[] = [];
  if (creating && !TASK_NAME.test(draft.name)) {
    const text = draft.name === '' ? 'Task name is empty' : 'Task name needs a-z, 0-9, . _ or -';
    missing.push({field: 'name', text});
  }
  if (draft.objective.trim() === '') missing.push({field: 'objective', text: 'Objective is empty'});
  if (draft.accuracy_command.trim() === '') {
    missing.push({field: 'accuracy', text: 'Accuracy command is empty'});
  }
  if (draft.benchmark_command.trim() === '') {
    missing.push({field: 'benchmark', text: 'Benchmark command is empty'});
  }
  if (draft.result_metric.trim() === '' || draft.result_json_argument.trim() === '') {
    missing.push({field: 'metric', text: 'Metric is incomplete'});
  }
  return missing.length > 0 ? missing : [{field: 'save', text: 'Save the task'}];
}

function taskBlockers({form, tasks, validation}: Readiness): Blocker[] {
  if (form.draft !== null) return draftBlockers(form.draft, form.task === NEW_TASK);
  if (validation?.project == null) return [];
  if (form.task === null) return [{field: 'task', text: 'Choose a task'}];
  const summary = tasks.find(task => task.name === form.task);
  return summary?.valid === false ? [{field: 'task', text: 'Task is invalid'}] : [];
}

function runBlockers({form, catalog, detail}: Readiness): Blocker[] {
  const found: Blocker[] = [];
  const loop = catalog.outer_loops.find(option => option.id === form.loop);
  const guided = form.draft === null && detail?.profile_guided === true;
  if (loop?.requires_profile_guided === true && !guided) {
    found.push({
      field: 'loop',
      text: 'Profile-guided needs a task with a [profile_guided] section',
    });
  }
  if (!WHOLE.test(form.budget.trim())) {
    found.push({field: 'budget', text: `${budgetLabel(loop)} must be a whole number`});
  }
  if (form.model.trim() === '') found.push({field: 'model', text: 'Model is empty'});
  return found;
}

function keyBlockers(provider: string, auth: AuthStatus): Blocker[] {
  const row = auth.providers.find(item => item.provider === provider);
  if (row === undefined || row.status !== 'missing') return [];
  const text =
    row.keys.length > 0
      ? `${row.display_name} key is needed`
      : `Sign in to ${row.display_name} from a terminal`;
  return [{field: 'key', text}];
}

/** Everything that keeps Start disabled, in form order. */
export function blockers(input: Readiness): Blocker[] {
  return [
    ...folderBlockers(input.form.path, input.validation),
    ...taskBlockers(input),
    ...runBlockers(input),
    ...keyBlockers(input.form.provider, input.auth),
  ];
}

export function roleSummary(form: SetupForm, roles: readonly string[]): string {
  const set = roles.filter(role => {
    const choice = form.roles[role];
    return choice !== undefined && (choice.model.trim() !== '' || choice.effort.trim() !== '');
  }).length;
  if (set === 0) return 'Use a different model per role';
  return set === 1 ? '1 role has its own model' : `${set} roles have their own model`;
}

/** How the task's result is read. Only editable tasks are known to be scalar maximize. */
export function resultText(detail: TaskDetail): string {
  switch (detail.result.kind) {
    case 'metric': {
      const metric = detail.result.metric ?? 'metric';
      return detail.editable ? `${metric}, higher is better` : metric;
    }
    case 'protocol':
      return 'reported through the result protocol';
    case 'none':
      return 'no result declared';
  }
}

export interface FolderStatus {
  tone: 'ok' | 'bad' | 'plain';
  text: string;
  /** Pending task files can be committed from here. */
  commit: boolean;
}

function dirtyStatus(pending: readonly string[]): FolderStatus {
  const others = pending.filter(path => !isTaskFile(path));
  const commit = others.length < pending.length;
  if (others.length === 0) return {tone: 'bad', text: 'Task files are not committed.', commit};
  const more = others.length > 2 ? ` and ${others.length - 2} more` : '';
  const named = `${others.slice(0, 2).join(', ')}${more}`;
  return {
    tone: 'bad',
    text: `Uncommitted changes in ${named}. Commit or stash them so rounds can revert cleanly.`,
    commit,
  };
}

function stateStatus(validation: ProjectValidation): FolderStatus {
  const line = (tone: FolderStatus['tone'], text: string): FolderStatus => ({
    tone,
    text,
    commit: false,
  });
  switch (validation.state) {
    case 'ready':
      return line('ok', 'Git repository, working tree clean');
    case 'missing':
      return line('bad', 'This folder does not exist.');
    case 'not_git':
      return line('bad', 'Not a git repository. Run git init and commit once, then check again.');
    case 'invalid':
      return line('bad', validation.message ?? 'The .vibesys folder here is not usable.');
    case 'uninitialized':
      return line('plain', 'No VibeSys tasks here yet. Create one below.');
    case 'no_tasks':
      return line('plain', 'No tasks yet. Create one below.');
    case 'no_commits':
      return {tone: 'bad', text: 'No commits yet. Commit the task files to start.', commit: true};
    case 'dirty_tree':
      return dirtyStatus(validation.pending);
  }
}

/** The line under the folder field; null before any check. */
export function folderStatus(
  validation: ProjectValidation | null,
  checking: boolean,
  error: string | null,
): FolderStatus | null {
  if (error !== null) return {tone: 'bad', text: error, commit: false};
  if (checking) return {tone: 'plain', text: 'Checking…', commit: false};
  return validation === null ? null : stateStatus(validation);
}

export type KeyWrite =
  | {kind: 'idle'}
  | {kind: 'saving'}
  | {kind: 'saved'; shadowed: boolean}
  | {kind: 'rejected'; message: string};

type Tone = 'ok' | 'bad' | 'warn' | 'plain';

export interface KeyView {
  label: string;
  /** The variable a pasted key is written to; null for a provider that signs in only through its CLI. */
  name: string | null;
  /** The field's hover hint. */
  where: string;
  placeholder: string;
  hint: string;
  tone: Tone;
  /** The terminal sign-in command, while a CLI-only provider is signed out. */
  login: string | null;
}

const PLACEHOLDERS: Record<ProviderAuth['status'], string> = {
  missing: 'Paste a key…',
  cli_session: 'Or paste a key…',
  key: 'Paste a new key to replace it…',
};
const SHADOWED_PLACEHOLDER = 'An environment variable is in use…';

// contract.py's KeyVar/ProviderAuth name neither the key's vendor nor a display label for it (only
// the variable itself), so the row is labeled from a small map of the allowlisted key variables
// (`_KEY_SUFFIXES` in entrypoints/web_home/keys.py) to the vendor that issues them.
const KEY_VENDORS: Record<string, string> = {
  OPENAI: 'OpenAI',
  ANTHROPIC: 'Anthropic',
  GEMINI: 'Gemini',
  GOOGLE: 'Google',
};
const KEY_SUFFIXES: readonly [suffix: string, kind: string][] = [
  ['_API_KEY', 'API key'],
  ['_AUTH_TOKEN', 'auth token'],
];

/** The row's label: the vendor the key belongs to, not the CLI it authenticates (task 5 review). */
function keyLabel(name: string | null, displayName: string): string {
  if (name !== null) {
    for (const [suffix, kind] of KEY_SUFFIXES) {
      if (name.endsWith(suffix)) {
        const vendor = KEY_VENDORS[name.slice(0, -suffix.length)];
        if (vendor !== undefined) return `${vendor} ${kind}`;
      }
    }
  }
  return `${displayName} key`;
}

function writeLine(write: KeyWrite, name: string): {hint: string; tone: Tone} | null {
  switch (write.kind) {
    case 'idle':
      return null;
    case 'saving':
      return {hint: 'Saving…', tone: 'plain'};
    case 'rejected':
      return {hint: `Rejected: ${write.message}`, tone: 'bad'};
    case 'saved':
      return write.shadowed
        ? {
            hint: `Saved, but ${name} in this app's environment wins. Unset it and restart VibeSys.`,
            tone: 'warn',
          }
        : {hint: 'Saved to .env. Unverified until the first run.', tone: 'ok'};
  }
}

function statusLine(row: ProviderAuth, cliOnly: boolean): {hint: string; tone: Tone} {
  const shadowed = row.keys.find(key => key.shadowed);
  if (shadowed !== undefined) {
    return {hint: `${shadowed.name} in this app's environment overrides .env.`, tone: 'warn'};
  }
  switch (row.status) {
    case 'key':
      return row.keys.some(key => key.source === 'env')
        ? {hint: 'Set in the environment. Unverified until the first run.', tone: 'ok'}
        : {hint: 'Saved in .env. Unverified until the first run.', tone: 'ok'};
    case 'cli_session':
      return {hint: 'Signed in through the CLI. Unverified until the first run.', tone: 'ok'};
    case 'missing':
      return cliOnly
        ? {hint: 'Sign in from a terminal, then check again:', tone: 'plain'}
        : {
            hint: 'Write-only. Stored in .env on this machine and never shown again.',
            tone: 'plain',
          };
  }
}

export function keyView(row: ProviderAuth, write: KeyWrite, dotenvPath: string): KeyView {
  const name = row.keys[0]?.name ?? null;
  const line = (name === null ? null : writeLine(write, name)) ?? statusLine(row, name === null);
  const shadowed = row.keys.some(key => key.shadowed);
  return {
    label: keyLabel(name, row.display_name),
    name,
    where: name === null ? row.login_command : `Written to ${name} in ${dotenvPath}`,
    placeholder: shadowed ? SHADOWED_PLACEHOLDER : PLACEHOLDERS[row.status],
    login: name === null && row.status === 'missing' ? row.login_command : null,
    ...line,
  };
}

const blank = (text: string): string | null => (text.trim() === '' ? null : text.trim());

export function startRequest(form: SetupForm, catalog: Catalog): StartRun {
  const roles: StartRun['roles'] = {};
  for (const role of catalog.outer_loops.find(loop => loop.id === form.loop)?.roles ?? []) {
    const choice = form.roles[role];
    const model = blank(choice?.model ?? '');
    const effort = blank(choice?.effort ?? '');
    if (model !== null || effort !== null) roles[role] = {model, reasoning_effort: effort};
  }
  return {
    task: form.task ?? '',
    outer_loop: form.loop,
    budget: Number(form.budget.trim()),
    compute_backend: form.compute,
    driver: form.driver,
    provider: form.provider,
    model: form.model.trim(),
    reasoning_effort: blank(form.effort),
    roles,
  };
}
