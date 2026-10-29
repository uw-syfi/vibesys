/**
 * The machine-scoped Settings store: which section of Settings is shown, which machine its
 * machine sections are about, and for each machine what it has set up (Slurm clusters, the Modal
 * token) and how each harness is signed in there. A pure reducer; every request carries a token
 * from the state's one counter and the state that awaits it keeps it, so a late reply (for a
 * machine read again since, a test of a cluster edited since, a removal the machine no longer
 * lists) is dropped.
 */
import type {
  AgentLogin,
  ClusterTest,
  ComputeSettings,
  FailureCause,
  LoginStart,
  MachineEntry,
  Offered,
  SlurmClusterSettings,
} from '@vibesys/ipc';
import {
  buildCluster,
  type ClusterFields,
  type ClusterForm,
  canSave,
  edited,
  NEW_FORM,
  problemOf,
  sentOf,
  tested,
} from './cluster-form.js';

/** A machine's key, as the app lists it. */
type HostKey = MachineEntry['host'];

/** The sections of Settings: two of a machine, three of the app. */
export type SettingsSection = 'clusters' | 'logins' | 'machines' | 'appearance' | 'notifications';

/** A reading of a machine: awaited, answered (the helper may predate it), or failed. */
export type Reading<T> =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'ready'; readonly offered: Offered<T>}
  | {readonly kind: 'failed'; readonly error: string; readonly cause?: FailureCause};

/** Why the helper did not start a login: what the contract lists. */
type UnavailableReason = Extract<LoginStart, {outcome: 'unavailable'}>['reason'];

/** Where starting a harness's own login stands. */
export type LoginProgress =
  | {readonly kind: 'starting'; readonly token: number}
  | {readonly kind: 'started'; readonly instruction: string | null}
  | {readonly kind: 'unavailable'; readonly reason: UnavailableReason}
  | {readonly kind: 'failed'; readonly error: string; readonly cause?: FailureCause};

export interface MachineView {
  readonly compute: Reading<ComputeSettings>;
  readonly logins: Reading<readonly AgentLogin[]>;
  readonly form: ClusterForm | null;
  /** The removal awaited, by cluster name. */
  readonly removing: {readonly name: string; readonly token: number} | null;
  /** Why the last removal failed, until the next. */
  readonly removeProblem: string | null;
  readonly starts: Readonly<Record<string, LoginProgress>>;
}

export interface MachineSettingsState {
  readonly section: SettingsSection;
  /** The machine chosen in the selector, or null until one is (the first listed is shown). */
  readonly machine: HostKey | null;
  /** The machines the app lists, in the selector's order. */
  readonly hosts: readonly HostKey[];
  readonly views: Readonly<Record<HostKey, MachineView>>;
  readonly lastToken: number;
}

type Outcome<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly cause?: FailureCause};

export type MachineSettingsAction =
  | {readonly type: 'section'; readonly section: SettingsSection}
  | {readonly type: 'machine'; readonly host: HostKey}
  | {readonly type: 'hosts'; readonly hosts: readonly HostKey[]}
  /** Settings was opened for `host`, on `section` (from New run's link to set a machine up). */
  | {readonly type: 'focus'; readonly host: HostKey; readonly section: SettingsSection}
  | {
      readonly type: 'refresh';
      readonly host: HostKey;
      readonly what: 'compute' | 'logins';
    }
  | {
      readonly type: 'computed';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<Offered<ComputeSettings>>;
    }
  | {
      readonly type: 'logins-read';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<Offered<readonly AgentLogin[]>>;
    }
  | {readonly type: 'open-form'; readonly host: HostKey}
  | {readonly type: 'close-form'; readonly host: HostKey}
  | {
      readonly type: 'field';
      readonly host: HostKey;
      readonly key: keyof ClusterFields;
      readonly value: string;
    }
  | {readonly type: 'test'; readonly host: HostKey}
  | {
      readonly type: 'tested';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<ClusterTest>;
    }
  | {readonly type: 'save'; readonly host: HostKey}
  | {
      readonly type: 'saved';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<ComputeSettings>;
    }
  | {readonly type: 'remove'; readonly host: HostKey; readonly name: string}
  | {
      readonly type: 'removed';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<ComputeSettings>;
    }
  | {readonly type: 'sign-in'; readonly host: HostKey; readonly provider: string}
  | {
      readonly type: 'signed-in';
      readonly token: number;
      readonly host: HostKey;
      readonly provider: string;
      readonly result: Outcome<LoginStart>;
    };

export type MachineSettingsCall =
  | {readonly kind: 'compute'; readonly host: HostKey; readonly token: number}
  | {readonly kind: 'logins'; readonly host: HostKey; readonly token: number}
  | {
      readonly kind: 'test';
      readonly host: HostKey;
      readonly token: number;
      readonly cluster: SlurmClusterSettings;
    }
  | {
      readonly kind: 'save';
      readonly host: HostKey;
      readonly token: number;
      readonly cluster: SlurmClusterSettings;
    }
  | {
      readonly kind: 'remove';
      readonly host: HostKey;
      readonly token: number;
      readonly name: string;
    }
  | {
      readonly kind: 'start-login';
      readonly host: HostKey;
      readonly token: number;
      readonly provider: string;
    };

export interface MachineSettingsStep {
  readonly state: MachineSettingsState;
  readonly requests: readonly MachineSettingsCall[];
}

export const INITIAL_MACHINE_SETTINGS: MachineSettingsState = {
  section: 'appearance',
  machine: null,
  hosts: [],
  views: {},
  lastToken: 0,
};

const none = (state: MachineSettingsState): MachineSettingsStep => ({state, requests: []});

function withView(
  state: MachineSettingsState,
  host: HostKey,
  change: (view: MachineView) => MachineView,
): MachineSettingsState {
  const view = state.views[host];
  if (view === undefined) return state;
  return {...state, views: {...state.views, [host]: change(view)}};
}

/** `host`'s view with both readings awaited, and the requests that make them. */
function reading(state: MachineSettingsState, host: HostKey): MachineSettingsStep {
  const computeToken = state.lastToken + 1;
  const loginsToken = computeToken + 1;
  const previous = state.views[host];
  const view: MachineView = {
    compute: {kind: 'loading', token: computeToken},
    logins: {kind: 'loading', token: loginsToken},
    form: previous?.form ?? null,
    removing: previous?.removing ?? null,
    removeProblem: null,
    starts: {},
  };
  return {
    state: {...state, views: {...state.views, [host]: view}, lastToken: loginsToken},
    requests: [
      {kind: 'compute', host, token: computeToken},
      {kind: 'logins', host, token: loginsToken},
    ],
  };
}

function readOne(
  state: MachineSettingsState,
  host: HostKey,
  what: 'compute' | 'logins',
): MachineSettingsStep {
  const token = state.lastToken + 1;
  const next = withView(state, host, view => ({...view, [what]: {kind: 'loading', token}}));
  return {state: {...next, lastToken: token}, requests: [{kind: what, host, token}]};
}

function settled<T>(current: Reading<T>, token: number, result: Outcome<Offered<T>>): Reading<T> {
  if (current.kind !== 'loading' || current.token !== token) return current;
  return result.ok ? {kind: 'ready', offered: result.value} : failed(result);
}

/** A failed outcome as it is kept: its words, and its typed cause when the connection's. */
function failed(result: {readonly error: string; readonly cause?: FailureCause}): {
  readonly kind: 'failed';
  readonly error: string;
  readonly cause?: FailureCause;
} {
  return {
    kind: 'failed',
    error: result.error,
    ...(result.cause === undefined ? {} : {cause: result.cause}),
  };
}

function startedLogin(result: Outcome<LoginStart>): LoginProgress {
  if (!result.ok) return failed(result);
  return result.value.outcome === 'started'
    ? {kind: 'started', instruction: result.value.instruction ?? null}
    : {kind: 'unavailable', reason: result.value.reason};
}

function withForm(
  state: MachineSettingsState,
  host: HostKey,
  form: ClusterForm | null,
): MachineSettingsState {
  return withView(state, host, view => ({...view, form}));
}

function startTest(
  state: MachineSettingsState,
  host: HostKey,
  form: ClusterForm,
): MachineSettingsStep {
  if (form.test.kind === 'testing') return none(state);
  const built = buildCluster(form.fields);
  if (!built.ok) return none(withForm(state, host, {...form, problem: built.problem}));
  const token = state.lastToken + 1;
  const next = {
    ...form,
    test: {kind: 'testing' as const, token, sent: sentOf(built.cluster)},
    problem: null,
  };
  return {
    state: {...withForm(state, host, next), lastToken: token},
    requests: [{kind: 'test', host, token, cluster: built.cluster}],
  };
}

function testReply(
  state: MachineSettingsState,
  host: HostKey,
  form: ClusterForm,
  action: Extract<MachineSettingsAction, {readonly type: 'tested'}>,
): MachineSettingsState {
  if (form.test.kind !== 'testing' || form.test.token !== action.token) return state;
  if (!action.result.ok) {
    const problem = problemOf(action.result.error);
    return withForm(state, host, {...form, test: {kind: 'untested'}, problem});
  }
  return withForm(state, host, tested(form, form.test.sent, action.result.value));
}

function startSave(
  state: MachineSettingsState,
  host: HostKey,
  form: ClusterForm,
): MachineSettingsStep {
  const built = buildCluster(form.fields);
  if (!canSave(form) || !built.ok) return none(state);
  const token = state.lastToken + 1;
  return {
    state: {...withForm(state, host, {...form, saving: token, problem: null}), lastToken: token},
    requests: [{kind: 'save', host, token, cluster: built.cluster}],
  };
}

function saveReply(
  state: MachineSettingsState,
  host: HostKey,
  form: ClusterForm,
  action: Extract<MachineSettingsAction, {readonly type: 'saved'}>,
): MachineSettingsState {
  if (form.saving !== action.token) return state;
  const {result} = action;
  if (!result.ok) {
    return withForm(state, host, {...form, saving: null, problem: problemOf(result.error)});
  }
  return withView(state, host, view => ({
    ...view,
    compute: {kind: 'ready', offered: {kind: 'offered', value: result.value}},
    form: null,
  }));
}

function stepForm(
  state: MachineSettingsState,
  host: HostKey,
  action: MachineSettingsAction,
): MachineSettingsStep {
  const form = state.views[host]?.form ?? null;
  if (action.type === 'open-form') return none(withForm(state, host, form ?? NEW_FORM));
  if (form === null) return none(state);
  switch (action.type) {
    case 'close-form':
      return none(form.saving === null ? withForm(state, host, null) : state);
    case 'field':
      return form.saving === null
        ? none(withForm(state, host, edited(form, action.key, action.value)))
        : none(state);
    case 'test':
      return startTest(state, host, form);
    case 'tested':
      return none(testReply(state, host, form, action));
    case 'save':
      return startSave(state, host, form);
    case 'saved':
      return none(saveReply(state, host, form, action));
    default:
      return none(state);
  }
}

function stepMachine(
  state: MachineSettingsState,
  action: Extract<MachineSettingsAction, {readonly host: HostKey}>,
): MachineSettingsStep {
  const {host} = action;
  const view = state.views[host];
  switch (action.type) {
    case 'refresh':
      return view === undefined ? reading(state, host) : readOne(state, host, action.what);
    case 'machine':
      return none({...state, machine: host});
    case 'focus':
      return none({...state, machine: host, section: action.section});
    case 'computed':
      return none(
        withView(state, host, current => ({
          ...current,
          compute: settled(current.compute, action.token, action.result),
        })),
      );
    case 'logins-read':
      return none(
        withView(state, host, current => ({
          ...current,
          logins: settled(current.logins, action.token, action.result),
          starts: {},
        })),
      );
    case 'remove': {
      if (view === undefined || view.removing !== null) return none(state);
      const token = state.lastToken + 1;
      return {
        state: {
          ...withView(state, host, current => ({
            ...current,
            removing: {name: action.name, token},
            removeProblem: null,
          })),
          lastToken: token,
        },
        requests: [{kind: 'remove', host, token, name: action.name}],
      };
    }
    case 'removed': {
      if (view?.removing?.token !== action.token) return none(state);
      const {result} = action;
      return none(
        withView(state, host, current => ({
          ...current,
          removing: null,
          ...(result.ok
            ? {compute: {kind: 'ready', offered: {kind: 'offered', value: result.value}}}
            : {removeProblem: result.error}),
        })),
      );
    }
    case 'sign-in': {
      if (view === undefined || view.starts[action.provider]?.kind === 'starting') {
        return none(state);
      }
      const token = state.lastToken + 1;
      return {
        state: {
          ...withView(state, host, current => ({
            ...current,
            starts: {...current.starts, [action.provider]: {kind: 'starting', token}},
          })),
          lastToken: token,
        },
        requests: [{kind: 'start-login', host, token, provider: action.provider}],
      };
    }
    case 'signed-in': {
      const progress = view?.starts[action.provider];
      if (progress?.kind !== 'starting' || progress.token !== action.token) return none(state);
      return none(
        withView(state, host, current => ({
          ...current,
          starts: {...current.starts, [action.provider]: startedLogin(action.result)},
        })),
      );
    }
    default:
      return stepForm(state, host, action);
  }
}

function stepCore(state: MachineSettingsState, action: MachineSettingsAction): MachineSettingsStep {
  if (action.type === 'section') return none({...state, section: action.section});
  if (action.type === 'hosts') return none({...state, hosts: action.hosts});
  if (action.type === 'focus') {
    return none({...state, machine: action.host, section: action.section});
  }
  return stepMachine(state, action);
}

/**
 * The next state and the requests to make, for `action` in `state`. Whenever a machine section is
 * shown for a machine not read yet, it is read: landing on a section, choosing a machine, and the
 * list of machines arriving all show what the machine has, never a blank.
 */
export function machineSettingsStep(
  state: MachineSettingsState,
  action: MachineSettingsAction,
): MachineSettingsStep {
  const core = stepCore(state, action);
  const shown = shownMachine(core.state);
  const machineSection = core.state.section === 'clusters' || core.state.section === 'logins';
  if (!machineSection || shown === null || core.state.views[shown] !== undefined) return core;
  const read = reading(core.state, shown);
  return {state: read.state, requests: [...core.requests, ...read.requests]};
}

/**
 * The machine the machine sections are about: the chosen one while the app lists it (or before it
 * lists any), else the first listed.
 */
export function shownMachine(state: MachineSettingsState): HostKey | null {
  const {machine, hosts} = state;
  if (machine !== null && (hosts.length === 0 || hosts.includes(machine))) return machine;
  return hosts[0] ?? null;
}

/** The tokens a view awaits. */
function awaited(view: MachineView): number[] {
  const tokens = [
    view.compute.kind === 'loading' ? view.compute.token : null,
    view.logins.kind === 'loading' ? view.logins.token : null,
    view.removing?.token ?? null,
    view.form?.saving ?? null,
    view.form?.test.kind === 'testing' ? view.form.test.token : null,
    ...Object.values(view.starts).map(start => (start.kind === 'starting' ? start.token : null)),
  ];
  return tokens.filter((token): token is number => token !== null);
}

/** What must hold in every state; empty when all do. */
export function machineSettingsInvariants(state: MachineSettingsState): string[] {
  return Object.entries(state.views).flatMap(([host, view]) => [
    ...awaited(view)
      .filter(token => token > state.lastToken)
      .map(token => `${host} awaits a token ${token} that was never issued`),
    ...(view.form?.saving != null && view.form.test.kind !== 'passed'
      ? [`${host} is saving a cluster that did not pass its test`]
      : []),
  ]);
}
