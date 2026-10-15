/**
 * The "Start a run" sheet's store: pick a project, list its tasks, pick one, optionally pick a
 * vetted model, start it.
 *
 * Facts that must agree live in one variant: the listed tasks carry the project they were listed
 * for, and editing the project drops them, so "Start" enabled for a task listed from another
 * project is not representable. The model is picked only from the pairs the host listed (or the
 * project default), never typed, and a host whose helper predates the choice offers none. A
 * task-list, model-list, or start reply applies only while its token is the one awaited. Cancel (or
 * Esc) while starting really cancels: the sheet says "Cancelling…", the main process stops the run
 * as soon as its record appears instead of attaching, and the window stays on the welcome view.
 */

import type {HostKey} from '@vibesys/hosts';
import type {
  HarnessChoice,
  HarnessListing,
  HarnessOffer,
  StartOutcome,
  WelcomeResult,
} from '@vibesys/ipc';
import {only, type Step} from '@vibesys/store';

interface SheetHost {
  readonly key: HostKey;
  readonly label: string;
}

/** The task list, always for the project it was asked for (`for`, trimmed). */
export type Tasks =
  | {readonly kind: 'idle'}
  | {readonly kind: 'loading'; readonly token: number; readonly for: string}
  | {
      readonly kind: 'listed';
      readonly for: string;
      readonly list: readonly string[];
      readonly picked: string | null;
    }
  | {readonly kind: 'failed'; readonly for: string; readonly error: string};

/**
 * The host's vetted provider and model pairs. `picked` null is the project default (its own
 * configuration); otherwise it is one of `list`. `unsupported`: the host's helper cannot take a
 * choice, so the sheet offers none.
 */
export type Harnesses =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'failed'; readonly error: string}
  | {
      readonly kind: 'listed';
      readonly list: readonly HarnessOffer[];
      readonly picked: HarnessChoice | null;
    };

interface Form {
  readonly host: SheetHost;
  readonly projects: readonly string[];
  readonly project: string;
  readonly args: string;
  readonly harnesses: Harnesses;
}

export type StartState =
  | {readonly kind: 'closed'; readonly next: number}
  | (Form & {
      readonly kind: 'editing';
      readonly next: number;
      readonly tasks: Tasks;
      /** Why the last start failed. */
      readonly error: string | null;
    })
  | (Form & {
      readonly kind: 'starting';
      readonly next: number;
      /** The list the task was picked from, shown again if the start fails. */
      readonly listed: Extract<Tasks, {kind: 'listed'}>;
      readonly task: string;
      readonly token: number;
    })
  | {
      readonly kind: 'cancelling';
      readonly next: number;
      readonly host: SheetHost;
      readonly task: string;
      readonly token: number;
    };

export type StartAction =
  /** Open the sheet on `host`, offering `projects`. */
  | {readonly type: 'open'; readonly host: SheetHost; readonly projects: readonly string[]}
  /** Cancel, Esc, or a click outside the sheet. */
  | {readonly type: 'close'}
  | {readonly type: 'project'; readonly value: string}
  | {readonly type: 'args'; readonly value: string}
  | {readonly type: 'list-tasks'}
  | {readonly type: 'pick'; readonly task: string}
  /** Pick a listed pair, or null for the project default. */
  | {readonly type: 'harness'; readonly value: HarnessChoice | null}
  | {readonly type: 'start'}
  /** The window now shows the run: an open form closes (a start in flight keeps waiting). */
  | {readonly type: 'run-shown'}
  | {
      readonly type: 'tasks-reply';
      readonly token: number;
      readonly result: WelcomeResult<readonly string[]>;
    }
  | {
      readonly type: 'harnesses-reply';
      readonly token: number;
      readonly result: WelcomeResult<HarnessListing>;
    }
  | {
      readonly type: 'start-reply';
      readonly token: number;
      readonly result: WelcomeResult<StartOutcome>;
    };

export type StartRequest =
  | {
      readonly kind: 'tasks';
      readonly token: number;
      readonly host: HostKey;
      readonly project: string;
    }
  | {readonly kind: 'harnesses'; readonly token: number; readonly host: HostKey}
  | {
      readonly kind: 'start';
      readonly token: number;
      readonly host: HostKey;
      readonly project: string;
      readonly task: string;
      readonly args: string;
      /** The picked pair; null keeps the project's configuration. */
      readonly harness: HarnessChoice | null;
    }
  | {readonly kind: 'cancel-start'; readonly token: number};

export const START_CLOSED: StartState = {kind: 'closed', next: 1};

/** The task Start would start: only a picked task of the list for the project shown. */
export function startable(state: StartState): string | null {
  if (state.kind !== 'editing') return null;
  const tasks = state.tasks;
  if (tasks.kind !== 'listed' || tasks.for !== state.project.trim()) return null;
  return tasks.picked !== null && tasks.list.includes(tasks.picked) ? tasks.picked : null;
}

function sameHarness(a: HarnessChoice, b: HarnessChoice): boolean {
  return a.provider === b.provider && a.model === b.model;
}

/** The pair a start carries: the picked one of the host's list, or null (the project default). */
export function chosenHarness(state: StartState): HarnessChoice | null {
  if (state.kind !== 'editing' && state.kind !== 'starting') return null;
  return state.harnesses.kind === 'listed' ? state.harnesses.picked : null;
}

/** The token of the reply the state awaits, by request slot. */
export function awaited(state: StartState): {
  tasks: number | null;
  harnesses: number | null;
  start: number | null;
} {
  const form = state.kind === 'editing' || state.kind === 'starting';
  return {
    tasks: state.kind === 'editing' && state.tasks.kind === 'loading' ? state.tasks.token : null,
    harnesses: form && state.harnesses.kind === 'loading' ? state.harnesses.token : null,
    start: state.kind === 'starting' || state.kind === 'cancelling' ? state.token : null,
  };
}

/** What must hold after every step; returns the violations (empty when none). */
export function startInvariants(state: StartState): string[] {
  const tasks = state.kind === 'editing' ? taskProblems(state) : [];
  const harnesses =
    state.kind === 'editing' || state.kind === 'starting' ? harnessProblems(state.harnesses) : [];
  return [...tasks, ...harnesses];
}

type Editing = Extract<StartState, {kind: 'editing'}>;
type Starting = Extract<StartState, {kind: 'starting'}>;

function taskProblems(state: Editing): string[] {
  const tasks = state.tasks;
  const problems: string[] = [];
  if (tasks.kind !== 'idle' && tasks.for !== state.project.trim()) {
    problems.push(`tasks listed for ${tasks.for} while the project is ${state.project}`);
  }
  if (tasks.kind === 'listed' && tasks.picked !== null && !tasks.list.includes(tasks.picked)) {
    problems.push(`picked ${tasks.picked} is not listed`);
  }
  return problems;
}

function harnessProblems(harnesses: Harnesses): string[] {
  if (harnesses.kind !== 'listed' || harnesses.picked === null) return [];
  const picked = harnesses.picked;
  if (harnesses.list.some(option => sameHarness(option, picked))) return [];
  return [`picked ${picked.provider} ${picked.model} is not a listed harness`];
}

function list(state: Editing): Step<StartState, StartRequest> {
  const project = state.project.trim();
  if (project === '') return only({...state, tasks: {kind: 'idle'}});
  const token = state.next;
  return {
    state: {...state, next: token + 1, tasks: {kind: 'loading', token, for: project}},
    requests: [{kind: 'tasks', token, host: state.host.key, project}],
  };
}

function pick(state: Editing, task: string): StartState {
  const tasks = state.tasks;
  if (tasks.kind !== 'listed' || !tasks.list.includes(task)) return state;
  return {...state, tasks: {...tasks, picked: task}};
}

function pickHarness(state: Editing, value: HarnessChoice | null): StartState {
  const harnesses = state.harnesses;
  if (harnesses.kind !== 'listed') return state;
  if (value === null) return {...state, harnesses: {...harnesses, picked: null}};
  const offered = harnesses.list.find(option => sameHarness(option, value));
  if (offered === undefined) return state;
  const picked = {provider: offered.provider, model: offered.model};
  return {...state, harnesses: {...harnesses, picked}};
}

function harnessesReply<S extends Editing | Starting>(
  state: S,
  token: number,
  result: WelcomeResult<HarnessListing>,
): S {
  const harnesses = state.harnesses;
  if (harnesses.kind !== 'loading' || harnesses.token !== token) return state;
  if (!result.ok) return {...state, harnesses: {kind: 'failed', error: result.error}};
  const listing = result.value;
  if (listing.kind === 'unsupported') return {...state, harnesses: {kind: 'unsupported'}};
  return {...state, harnesses: {kind: 'listed', list: listing.harnesses, picked: null}};
}

function start(state: Editing): Step<StartState, StartRequest> {
  const task = startable(state);
  if (task === null || state.tasks.kind !== 'listed') return only(state);
  const token = state.next;
  const {host, projects, project, args, harnesses} = state;
  const listed = state.tasks;
  const harness = chosenHarness(state);
  return {
    state: {
      kind: 'starting',
      next: token + 1,
      host,
      projects,
      project,
      args,
      harnesses,
      listed,
      task,
      token,
    },
    requests: [
      {kind: 'start', token, host: host.key, project: project.trim(), task, args, harness},
    ],
  };
}

function tasksReply(
  state: Editing,
  token: number,
  result: WelcomeResult<readonly string[]>,
): StartState {
  const tasks = state.tasks;
  if (tasks.kind !== 'loading' || tasks.token !== token) return state;
  if (!result.ok) return {...state, tasks: {kind: 'failed', for: tasks.for, error: result.error}};
  const [first, ...rest] = result.value;
  const picked = first !== undefined && rest.length === 0 ? first : null;
  return {...state, tasks: {kind: 'listed', for: tasks.for, list: result.value, picked}};
}

function editing(state: Editing, action: StartAction): Step<StartState, StartRequest> {
  switch (action.type) {
    case 'close':
    case 'run-shown':
      return only({kind: 'closed', next: state.next});
    case 'project':
      // The listed tasks belong to the project they were read from: editing it drops them.
      return only({...state, project: action.value, tasks: {kind: 'idle'}, error: null});
    case 'args':
      return only({...state, args: action.value});
    case 'list-tasks':
      return list({...state, error: null});
    case 'pick':
      return only(pick(state, action.task));
    case 'harness':
      return only(pickHarness(state, action.value));
    case 'start':
      return start(state);
    case 'tasks-reply':
      return only(tasksReply(state, action.token, action.result));
    case 'harnesses-reply':
      return only(harnessesReply(state, action.token, action.result));
    default:
      return only(state);
  }
}

function starting(state: Starting, action: StartAction): Step<StartState, StartRequest> {
  if (action.type === 'close') {
    const {next, host, task, token} = state;
    return {
      state: {kind: 'cancelling', next, host, task, token},
      requests: [{kind: 'cancel-start', token}],
    };
  }
  if (action.type === 'harnesses-reply') {
    // The list belongs to the form, which a failed start shows again.
    return only(harnessesReply(state, action.token, action.result));
  }
  if (action.type !== 'start-reply' || action.token !== state.token) return only(state);
  if (action.result.ok) return only({kind: 'closed', next: state.next});
  const {next, host, projects, project, args, harnesses, listed} = state;
  return only({
    kind: 'editing',
    next,
    host,
    projects,
    project,
    args,
    harnesses,
    tasks: listed,
    error: action.result.error,
  });
}

function opened(
  state: StartState,
  host: SheetHost,
  projects: readonly string[],
): Step<StartState, StartRequest> {
  if (state.kind !== 'closed') return only(state);
  const token = state.next;
  const listed = list({
    kind: 'editing',
    next: token + 1,
    host: {key: host.key, label: host.label},
    projects,
    project: projects[0] ?? '',
    args: '',
    harnesses: {kind: 'loading', token},
    tasks: {kind: 'idle'},
    error: null,
  });
  return {
    state: listed.state,
    requests: [{kind: 'harnesses', token, host: host.key}, ...listed.requests],
  };
}

export function startStep(state: StartState, action: StartAction): Step<StartState, StartRequest> {
  if (action.type === 'open') return opened(state, action.host, action.projects);
  switch (state.kind) {
    case 'closed':
      return only(state);
    case 'editing':
      return editing(state, action);
    case 'starting':
      return starting(state, action);
    case 'cancelling':
      // Whatever the start's answer (cancelled, failed, or attached before the cancel arrived),
      // the sheet's work is over.
      if (action.type !== 'start-reply' || action.token !== state.token) return only(state);
      return only({kind: 'closed', next: state.next});
  }
}
